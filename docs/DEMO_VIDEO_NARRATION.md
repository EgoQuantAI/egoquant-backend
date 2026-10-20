# EgoQuant demo video: narration (about 5 minutes)

0:00 Introduction
Hello, and welcome to EgoQuant: an AI-powered wealth advisor that builds a portfolio around who you really are, and explains every decision behind it. It is built on the idea that a good adviser should do four things: profile the client, compare products, build a suitable allocation, and explain it clearly. However, current robo-advisers rarely explain their reasoning, and chatbots rarely check whether advice is suitable. We tried to solve this problem with EgoQuant.

0:24 How it decides
One important component of our engine is that at EgoQuant deterministic code makes every decision. Four specialised agents we call the red team do the work, an independent Risk Engine checks its decisions, and our AI advisor explains the results to the clients. We build trust by ensuring that AI never makes the decision, and nothing can move your money. Let's walk through it with a fake profile of Lina, who is investing fifty thousand dollars for a home in eight years.

0:52 Risk profile
Lina starts by creating her account with a username, her email and a strong password, and confirms it from her inbox. Before anything is stored, she gives separate consent for each use of her data, and she can withdraw it or delete everything with one click. Her data is locked to her alone: no other user can read it.
Then she completes a validated risk questionnaire. It measures three things: how much risk she is willing to take, using the published Grable and Lytton scale; how much loss she can actually afford, based on her goal and savings; and her financial knowledge. Code scores it and places her on the C1 to C5 scale used in Chinese suitability rules. Her attitude says C4, but her finances limit her to C3, and the screen tells her exactly why.

1:37 Investments
Next, she chooses what she would like to invest in. Anything her risk class doesn't allow, like crypto, is shown but locked, with the reason. These choices only decide which real market data our agents fetch. They never change her risk class. She can also upload her Interactive Brokers statement, so we can read her previous trade log and personalize her portfolio accordingly.

1:58 Risk Game
Then she plays a short risk game: a few fifty-fifty gambles and a market-crash test. Why a game? Because people often say one thing and do another. The questionnaire captures what Lina says. The game and her trading history show what she does. Agent B, her behavioural twin, uses that evidence to tighten her investment limits.

2:17 The portfolio
Now EgoQuant builds her portfolio on real market data. Agent C fetches data on the funds she is allowed to hold. Agent A builds one hundred candidate portfolios inside her limits, including the tighter limits from Agent B, so nothing that doesn't fit her personal profile gets through. Each portfolio is tested across ten thousand simulated futures, drawn from twenty years of real returns, with a Hidden Markov Model. The portfolio giving her the best chance of reaching her goal wins. Then the independent Risk Engine re-checks every limit, and approves the winning portfolio.
Her portfolio report explains every suggested holding and why it is there, each limit and where she stands, what a crash would cost her in dollars, and her fees. She has a sixty-four in a hundred chance of reaching her goal, and she can download the full report in one click. Alongside it, she sees the news on every fund she owns and whether it is good or bad for holders of that fund. Agent D collects trusted financial news on Lina's funds every fifteen minutes and uses a fund-aware AI model to rate each headline for holders of that fund.

3:22 Advisory dialogue
She can also ask questions about her portfolio with the Ask the Advisor button. Let's ask: why should she hold bonds? The advisor answers in plain words, using only the facts our engine produced. If she asks about a suggestion that wasn't made by the red team, the reply is blocked. She can also ask what-if questions, like what if she needs twenty thousand back in three years? The red team re-runs its simulations, and the advisor communicates the results. And if she tries to inject prompts into the chat, the compliance filter withholds her question and tells her why.

3:55 Trade check and audit trail
Before Lina trades, she can check a planned trade. In this sandbox account, with a simulated brokerage statement, let's ask if we should buy five thousand dollars of VTI. Our red team gives a verdict: here it is advised, but with warnings based on her brokerage statement. If she decides to go through with the trade, every step lands in her activity log. EgoQuant never places an order, but she can ask for personal advice at every step of her investment journey.

4:23 On-chain seal
Finally, when Lina confirms her plan, EgoQuant seals it on a public blockchain, the Ethereum Sepolia test network. Only a short fingerprint of her exact report goes on-chain, never her data. Anyone can upload the report on our Verify page: if a single number was changed, it no longer matches. So no one, not even us, can quietly rewrite her advice.

4:43 Closure
So by our engine, traders of any background can be profiled honestly, given a portfolio that fits how they really behave, and receive trustworthy suggestions. To ensure this smooth and secure workflow, we ran more than thirty-five hundred automated tests on every function of EgoQuant and refined our system.
Thank you!
