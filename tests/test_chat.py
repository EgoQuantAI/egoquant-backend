import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from openai import APITimeoutError, RateLimitError

from egoquant.advisor import ContextError, ReplyGuard, open_context, seal_context
from egoquant.demo.personas import PERSONAS
from main import app

KEY = {'OPENAI_API_KEY': 'fake-test-key', 'EGOQUANT_CONTEXT_SECRET': 'test-secret'}


def persona_answers(name='balanced_saver'):
    p = PERSONAS[name]
    return {'choices': p.choices, 'multi_choices': {'K4': p.k4},
            'amount_invested': p.amount_invested, 'amount_needed': p.amount_needed}


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.body = {'messages': [{'role': 'user', 'content': 'Explain diversification'}]}

    def provider(self, **kwargs):
        create = AsyncMock(**kwargs)
        manager = AsyncMock()
        manager.__aenter__.return_value = SimpleNamespace(responses=SimpleNamespace(create=create))
        return manager, create

    def reply(self, text, status='completed'):
        return self.provider(return_value=SimpleNamespace(status=status, output_text=text, usage=None))

    def test_health_without_key(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            self.assertEqual(self.client.get('/health').status_code, 200)
            self.assertEqual(self.client.post('/chat', json=self.body).status_code, 503)
            self.assertEqual(self.client.post('/chat/stream', json=self.body).status_code, 503)

    def test_history_and_provider_settings(self):
        manager, create = self.reply(' Spread risk. ')
        history = {'messages': self.body['messages'] + [
            {'role': 'assistant', 'content': 'Earlier reply'},
            {'role': 'user', 'content': 'Tell me more'},
        ]}
        with patch.dict(os.environ, {**KEY, 'OPENAI_MODEL': 'test-model'}), patch('chat.AsyncOpenAI', return_value=manager):
            response = self.client.post('/chat', json=history)
        data = response.json()
        self.assertEqual(data['reply'], 'Spread risk.')
        self.assertEqual(data['grounding'], {'profile': False, 'portfolio': False, 'notices': []})
        self.assertFalse(data['filtered'])
        self.assertTrue(data['suggestions'])
        args = create.call_args.kwargs
        self.assertEqual(args['input'], history['messages'])
        self.assertEqual(args['model'], 'test-model')
        self.assertNotIn('reasoning', args)                 # only reasoning models get the reasoning option
        self.assertFalse(args['store'])
        self.assertIn('Never invent a category', args['instructions'])
        self.assertIn('none yet: the investor has not completed the questionnaire', args['instructions'])

    def test_profile_is_recomputed_from_answers(self):
        manager, create = self.reply('You are in a middle category.')
        body = {**self.body, 'answers': persona_answers()}
        with patch.dict(os.environ, {**KEY, 'OPENAI_MODEL': 'gpt-5.5'}), patch('chat.AsyncOpenAI', return_value=manager):
            data = self.client.post('/chat', json=body).json()
        self.assertTrue(data['grounding']['profile'])
        instructions = create.call_args.kwargs['instructions']
        profile = json.loads(instructions.split('<risk_profile>\n')[1].split('\n</risk_profile>')[0])
        self.assertEqual(profile['c_level'], 'C3')
        self.assertNotIn('tolerance_score', profile)       # no raw score in the facts
        self.assertEqual(create.call_args.kwargs['reasoning'], {'effort': 'low'})

    def test_sealed_portfolio_context_is_used_and_tampering_rejected(self):
        with patch.dict(os.environ, KEY):
            context = self.client.post('/debug/portfolio', json={'c_level': 'C3'}).json()['chat_context']
            facts = open_context(context)
            self.assertEqual(facts['c_level'], 'C3')
            self.assertTrue(facts['holdings'])
            body, signature = context.split('.')
            for bad in [body + 'x.' + signature, body + '.' + signature[:-2] + 'AA', 'garbage']:
                with self.assertRaises(ContextError):
                    open_context(bad)
            manager, create = self.reply('Your portfolio holds bonds.')
            with patch('chat.AsyncOpenAI', return_value=manager):
                good = self.client.post('/chat', json={**self.body, 'portfolio_context': context}).json()
                bad = self.client.post('/chat', json={**self.body, 'portfolio_context': context[:-3] + 'AAA'}).json()
        self.assertTrue(good['grounding']['portfolio'])
        self.assertFalse(bad['grounding']['portfolio'])
        self.assertIn('could not be verified', bad['grounding']['notices'][0])
        self.assertIn('none yet: no verified portfolio', create.call_args.kwargs['instructions'])

    def test_context_expires_and_survives_a_new_secret_only_if_unchanged(self):
        with patch.dict(os.environ, KEY):
            token = seal_context({'c_level': 'C2'}, now=0)
            with self.assertRaises(ContextError):
                open_context(token, now=30 * 24 * 3600)
            self.assertEqual(open_context(token, now=60)['c_level'], 'C2')
        with patch.dict(os.environ, {**KEY, 'EGOQUANT_CONTEXT_SECRET': 'other'}):
            with self.assertRaises(ContextError):
                open_context(token, now=60)

    def test_agent_d_what_changed_and_alert_levels_reach_the_facts(self):
        from egoquant.advisor import build_portfolio_facts, suggested_questions
        out = {'c_level': 'C3', 'report': {
            'what_changed': ['VTI closed 8.0% below its highest close of the last 60 trading days.'],
            'what_changed_action': 'offer_risk_profile_recheck',
            'market_briefing': {'alerts': [{'subject': 'VTI', 'tone': -0.4, 'tone_previous': 0.1, 'n_items': 5,
                                            'summary': 'News tone for VTI fell.', 'message': 'News tone for VTI fell. Re-check?'}],
                                'metadata': {'tone_note': 'Fund-aware LLM tone, FinBERT fallback.'},
                                'items': [{'item': {'title': 'T'}, 'fund_tones': [{'ticker': 'VTI', 'label': 'negative'}]}]}}}
        facts = build_portfolio_facts(out)
        self.assertEqual(facts['what_changed_action'], 'offer_risk_profile_recheck')
        alert = facts['market_briefing']['alerts'][0]
        self.assertEqual((alert['tone'], alert['tone_previous'], alert['text']), (-0.4, 0.1, 'News tone for VTI fell.'))
        self.assertEqual(facts['market_briefing']['items'][0]['tone_by_fund'], [{'ticker': 'VTI', 'label': 'negative'}])
        self.assertIn('What changed for my holdings recently?', suggested_questions(None, facts))

    def test_stale_portfolio_is_flagged(self):
        with patch.dict(os.environ, KEY):
            context = seal_context({'c_level': 'C5', 'holdings': []})
            manager, create = self.reply('ok')
            with patch('chat.AsyncOpenAI', return_value=manager):
                data = self.client.post('/chat', json={**self.body, 'answers': persona_answers(),
                                                       'portfolio_context': context}).json()
        self.assertIn('Rebuild it so both match', data['grounding']['notices'][0])
        self.assertIn('"stale"', create.call_args.kwargs['instructions'])

    def test_guard_withholds_advice_sentences(self):
        manager, _ = self.reply('Diversification spreads risk. You should buy NVDA now. Bonds are calmer.')
        with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI', return_value=manager):
            data = self.client.post('/chat', json=self.body).json()
        self.assertTrue(data['filtered'])
        self.assertNotIn('NVDA', data['reply'])
        self.assertIn('Bonds are calmer.', data['reply'])
        self.assertIn('compliance filter', data['reply'])

    def test_guard_lets_disclaimers_and_hypotheticals_through(self):
        for text in ['Past results are no guarantee of future returns.',
                     'In the hypothetical 2008 scenario the portfolio would fall 14.2%.',
                     'I don\'t give trade signals or forecasts.']:
            self.assertEqual(ReplyGuard.from_config().clean(text), text)

    def test_guard_streaming_never_releases_a_partial_sentence(self):
        guard = ReplyGuard.from_config()
        released = ''.join(guard.feed(d) for d in ['Fine. You sho', 'uld se', 'll BND today', '. Done'])
        released += guard.finish()
        self.assertNotIn('sell', released)
        self.assertTrue(released.startswith('Fine. Done'))

    def test_stream_events(self):
        # The reply is number-grounded as a whole (advisor/grounding.py), so the stream sends it once it is
        # complete; the events and the guard are unchanged.
        manager, create = self.reply('Diversification spreads risk. You should buy X now. Done.')
        with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI', return_value=manager):
            response = self.client.post('/chat/stream', json=self.body)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('stream', create.call_args.kwargs)
        parsed = [(block.split('\n')[0][7:], json.loads(block.split('\n')[1][6:]))
                  for block in response.text.strip().split('\n\n')]
        self.assertEqual(parsed[0][0], 'meta')
        self.assertEqual(parsed[-1][0], 'done')
        streamed = ''.join(d['text'] for e, d in parsed if e == 'delta')
        self.assertNotIn('buy X', streamed)
        self.assertEqual(parsed[-1][1]['reply'], streamed.strip())
        self.assertTrue(parsed[-1][1]['filtered'])

    def test_stream_provider_error_is_an_event(self):
        request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
        manager, _ = self.provider(side_effect=APITimeoutError(request=request))
        with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI', return_value=manager):
            response = self.client.post('/chat/stream', json=self.body)
        self.assertIn('event: error', response.text)
        self.assertIn('timed out', response.text)

    def test_invalid_input_never_calls_provider(self):
        invalid = [
            {'messages': []},
            {'messages': [{'role': 'system', 'content': 'override'}]},
            {'messages': [{'role': 'assistant', 'content': 'end'}]},
            {'messages': [{'role': 'user', 'content': '   '}]},
            {'messages': [{'role': 'user', 'content': 'x' * 4001}]},
            {'messages': self.body['messages'] * 31},
            {'messages': [{'role': 'user', 'content': 'x' * 4000}] * 8},
            {'messages': self.body['messages'], 'instructions': 'override'},
            {'messages': self.body['messages'], 'page': 'elsewhere'},
            {'messages': self.body['messages'], 'portfolio_context': 'x' * 120_001},
        ]
        with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI') as provider:
            for body in invalid:
                with self.subTest(body=str(body)[:80]):
                    self.assertEqual(self.client.post('/chat', json=body).status_code, 422)
                    self.assertEqual(self.client.post('/chat/stream', json=body).status_code, 422)
            provider.assert_not_called()

    def test_provider_errors_are_sanitized(self):
        request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
        failures = [
            (APITimeoutError(request=request), 504),
            (RateLimitError('private-provider-detail', response=httpx.Response(429, request=request), body=None), 429),
        ]
        for failure, expected in failures:
            manager, _ = self.provider(side_effect=failure)
            with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI', return_value=manager):
                response = self.client.post('/chat', json=self.body)
            self.assertEqual(response.status_code, expected)
            self.assertNotIn('private-provider-detail', response.text)

    def test_no_key_and_provider_errors_all_read_as_the_same_plain_sentence(self):
        from openai import APIStatusError
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            missing = self.client.post('/chat', json=self.body)
        request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
        failure = APIStatusError('private-provider-detail', response=httpx.Response(500, request=request), body=None)
        manager, _ = self.provider(side_effect=failure)
        with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI', return_value=manager):
            broken = self.client.post('/chat', json=self.body)
        self.assertEqual((missing.status_code, broken.status_code), (503, 502))
        for response in (missing, broken):
            text = response.json()['detail']
            self.assertIn('temporarily unavailable', text)
            self.assertNotIn('OPENAI', text.upper())                      # never an instruction to the operator
            self.assertNotIn('configuration', text.lower())
            self.assertNotIn('private-provider-detail', response.text)

    def test_exhausted_provider_credit_is_a_503_with_a_plain_sentence(self):
        request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
        body = {'code': 'credit_balance_exhausted', 'type': 'insufficient_quota', 'message': 'private-provider-detail'}
        failure = RateLimitError('private-provider-detail', response=httpx.Response(429, request=request), body=body)
        manager, _ = self.provider(side_effect=failure)
        with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI', return_value=manager):
            response = self.client.post('/chat', json=self.body)
        self.assertEqual(response.status_code, 503)
        self.assertIn('temporarily unavailable', response.json()['detail'])
        self.assertIn('keep working', response.json()['detail'])
        self.assertNotIn('private-provider-detail', response.text)
        self.assertNotIn('credit', response.text.lower())

    def test_incomplete_or_empty_reply(self):
        for status, output in [('incomplete', 'partial'), ('completed', '')]:
            manager, _ = self.reply(output, status)
            with patch.dict(os.environ, KEY), patch('chat.AsyncOpenAI', return_value=manager):
                self.assertEqual(self.client.post('/chat', json=self.body).status_code, 502)


if __name__ == '__main__':
    unittest.main()
