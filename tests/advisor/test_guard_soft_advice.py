"""The compliance guard withholds the softer ways of telling someone what to do with a security, in English and in Chinese, and lets
neutral sentences through (compliance review of the advisor prompt, 20 Oct: the old list missed 14 of 23 advice-like sentences).

Two sides are pinned: every sentence that reads like advice is withheld, and the sentences the advisor MUST be able to say
(a licensed adviser, where to add Stocks in the app, a loss in a scenario, "not a recommendation") are not."""

import pytest

from egoquant.advisor.guard import ReplyGuard

SOFT_ADVICE = [
    "You could sell half of your VTI.",
    "You might want to buy NVDA.",
    "You can sell some of it if you like.",
    "Consider selling some of it.",
    "It would be fine to go ahead.",
    "It is perfectly reasonable to sell.",
    "It's safe to hold on.",
    "I'd suggest waiting.",
    "I would suggest selling half.",
    "NVDA is worth buying.",
    "It is safe to sell.",
    "You shouldn't sell at the bottom.",
    "You should hold on to it.",
    "You should wait until the price drops.",
    "Feel free to buy more.",
    "Go ahead and buy it.",
    "That's a great move.",
    "This is a good time to buy.",
    "There is no harm in buying more.",
    # found by the compliance re-check of the candidate (Bartu, 20 Oct)
    "You can safely sell half of your VTI.",
    "You can sell half of your VTI if you like.",
    "Prices will rise after the results.",
    "NVDA is likely to rally next month.",
    "NVDA will go up soon.",
    "I recommend keeping the bonds.",
    "I suggest staying in cash.",
    "I would suggest keeping an emergency fund.",
    "Selling now would be wise.",
    "It will be a good idea to add some gold.",
    "It would be wise to sell before the results.",
]
SOFT_ADVICE_ZH = [
    "你应该卖出这只基金。",
    "建议你买入英伟达。",
    "现在是买入的好时机。",
    "这只基金稳赚不赔。",
    "我们保证收益。",
    "你最好加仓。",
]
NEUTRAL = [
    "I'd suggest talking to a licensed adviser about this.",
    "You can add Stocks under Investments to include them.",
    "You could see a loss of 14.0% in that scenario.",
    "This is not a recommendation to trade.",
    "You should wait a few minutes before the next check.",
    "Feel free to go to the Portfolio page.",
    "Go ahead and tell me the amount you have in mind.",
    "You can change that by retaking the questionnaire.",
    "EgoQuant's check finds this trade within your limits, with two warnings.",
    "I don't give trade signals or forecasts.",
    "That is a good question.",
    "The simulation shows adding 60 a month would raise the chance from 62 to 71 of every 100 simulated futures.",
    "Consider the uncertainty in the history halves.",
    "There is no guarantee of returns.",
    # what a category allows and where things are: the advisor must be able to say these (the old pattern blocked them)
    "Under C2 you can buy bond funds and cash funds, but not stocks.",
    "These are the kinds of investments you can buy on Fidelity.",
    "You can sell the position at any time; the check only compares the trade with your limits.",
    "It would be wise to ask a licensed adviser about tax.",
    "In that scenario the portfolio would fall about 14% (hypothetical).",
    "In that scenario the portfolio will fall about 14%.",
    "Adding 300 a month would raise the chance from 86 to 100 of every 100 simulated futures.",
]
NEUTRAL_ZH = [
    "本工具不提供买卖建议，也不保证收益。",
    "过去的数据不代表未来，没有稳赚不赔的投资。",
    "这是一个基于历史数据的模拟，不是预测。",
]


@pytest.mark.parametrize("sentence", SOFT_ADVICE + SOFT_ADVICE_ZH)
def test_advice_like_sentences_are_withheld(sentence):
    guard = ReplyGuard.from_config()
    out = guard.clean(sentence)
    assert guard.hits, sentence
    assert sentence not in out, sentence
    assert "withheld" in out


@pytest.mark.parametrize("sentence", NEUTRAL + NEUTRAL_ZH)
def test_neutral_sentences_pass_unchanged(sentence):
    guard = ReplyGuard.from_config()
    assert guard.clean(sentence) == sentence
    assert not guard.hits


def test_a_chinese_reply_is_checked_sentence_by_sentence():
    """。！？ end a Chinese sentence with no space after: the advice sentence goes, the neutral one next to it stays."""
    guard = ReplyGuard.from_config()
    out = guard.clean("你应该卖出这只基金。这是一个基于历史数据的模拟，不是预测。")
    assert "你应该卖出" not in out
    assert "这是一个基于历史数据的模拟，不是预测。" in out


def test_streamed_chinese_text_is_released_per_sentence_and_still_checked():
    guard = ReplyGuard.from_config()
    released = ""
    for chunk in ["模拟显示，", "目标的机会是 68 / 100。", "建议你买入", "英伟达。", "以上不是预测。"]:
        released += guard.feed(chunk)
    released += guard.finish()
    assert "建议你买入" not in released
    assert "目标的机会是 68 / 100。" in released and "以上不是预测。" in released


def test_the_old_forbidden_patterns_still_work():
    for sentence in ("You should definitely buy VTI now.", "It's guaranteed to grow.", "The price target is 400.", "Buy the dip."):
        guard = ReplyGuard.from_config()
        assert sentence not in guard.clean(sentence) and guard.hits, sentence


def test_a_mixed_reply_keeps_the_good_sentences():
    guard = ReplyGuard.from_config()
    text = ("EgoQuant's check finds this trade within your limits. You could sell half of your VTI. "
            "It only compares the trade with your limits.")
    out = guard.clean(text)
    assert "within your limits" in out and "It only compares the trade with your limits." in out
    assert "You could sell half" not in out
