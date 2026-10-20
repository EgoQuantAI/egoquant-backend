# EgoQuant: try the on-chain Seal (guide for judges)

About 5 minutes. When an investor confirms an approved plan, EgoQuant publishes a **fingerprint** of the advice on the
Sepolia **test** blockchain. Anyone can later check that the report was not changed. No real money is involved, and no
personal data goes on-chain.

## Without an account (30 seconds)

1. Click **Verify a report** (left sidebar, with the shield).
2. Click **Use the demo report** → **Unchanged ✓ · Sealed for: demo_investor · confirmed … · block N**, followed by **the
   original sealed report** itself. *(A real EgoQuant report for an invented investor was sealed in that block.)*
3. Click **View on Etherscan** → the public transaction: 0 ETH, from EgoQuant's sealing address to itself, **Input Data**
   starting `0x45474f5131` ("EGOQ1") followed by the 32-byte fingerprint.
4. Click **Try a tampered copy** (one weight shown in the report becomes 99.9%) → **Does not match ✗: this report was
   changed after it was sealed**.

## With an account: seal your own plan

1. **Sign in.** Sidebar → **Sign in · Account** → **Sign up** with an email, a password and a username (or use the demo
   account the team gave you). If a confirmation email comes, click its link and then sign in.
2. **Profile.** Answer the questionnaire (**I. Profile**). *Your answers set your risk category (C1–C5).*
3. **Investments.** Pick what you would like to hold (**II. Investments**). The Risk Game (III) is optional.
4. **Portfolio.** Open **IV. Portfolio**, enter a goal amount, and click **Build Portfolio**. *The plan is built on
   market data and re-checked by an independent Risk Engine ("Approved").* It is saved to your account.
5. **Wait for the news.** At the top the button reads **Waiting for the news rating to finish (N of M)…**. *Every
   headline is rated first, because the seal covers the finished report.* Then it reads **Not sealed · Confirm and seal**.
6. **Confirm and seal.** Read the line above the button, click it, read the text, tick **I have read this and I agree**,
   and click **Confirm and seal**. *The exact report, marked "Sealed for" your username, is fingerprinted; only the
   fingerprint is published.*
7. **Sealed.** **Sealing… (waiting for a block)** changes, usually within a minute, to **Sealed on Sepolia ✓ · block N ·
   date · View on Etherscan ↗**, with **Download sealed report** (and a small **Technical seal file (.json)**).
8. **Etherscan.** **View on Etherscan ↗** → the transaction: Success, 0 ETH, Input Data `0x45474f5131…`.
9. **Download.** **Download sealed report** saves one `.html` file: the report exactly as sealed (its header says "Sealed
   for: <you>"), followed by the **Seal** section with the transaction, block, date and how to check it. (The plain
   **Download report** button gives the report without its seal; use **Download sealed report** for checking.)
10. **Verify.** Sidebar → **Verify a report** → **Upload a sealed report** → choose that file → **Unchanged ✓ · Sealed
    for: <you>**, followed by the original. Change any number in the file with a text editor and upload it again:
    **Does not match ✗: this report was changed after it was sealed**.
11. **Account.** **Account** → *Reports*: the same report shows **Sealed on Sepolia ✓**, and **My seals** lists every
    plan you sealed with its transaction (to compare with any sealed report someone shows you). **Build and save a report**
    there works too: "Rating news… N of M", then **Not sealed · Confirm and seal** on the new row.

## What each screen means

| Screen | In one line |
|---|---|
| Approved C3 | The independent Risk Engine checked the plan against your limits and approved it. |
| Waiting for the news rating to finish (N of M)… | Headlines are being rated for your funds; the seal waits so it covers the finished report. |
| Not sealed · Confirm and seal | The finished report is saved and can be confirmed. |
| Sealing… (waiting for a block) | The fingerprint was sent to the test network and is waiting to be included in a block. |
| Sealed on Sepolia ✓ · block N | The fingerprint is fixed in a public block: from now on, any change to the report is detectable. |
| Unchanged ✓ · Sealed for: X | This is exactly the report that was confirmed by the account X, and its fingerprint is on the blockchain. |
| Does not match ✗: this report was changed after it was sealed | Something in the report was changed. |
| Does not match ✗: the seal doesn't match the blockchain | The seal data was forged or belongs to no EgoQuant transaction. |
| This report was never sealed. | A report without a seal (it was never confirmed). |

## Limits

- **5 seals per account per day** (200 a day in total). Past that the plan is still confirmed, and the seal waits until
  the next day.
- Sepolia is a public **test** network: no real money. The seal proves what was confirmed, for whom and when. It does not
  prove the advice was right.
- A printed or PDF copy can't prove itself: check the sealed report (.html) or the seal file at **Verify a report**.
- A public blockchain entry can never be deleted. "Delete my data" deletes the record, its salt and the sealed document, so we can no longer link
  the fingerprint to anyone (a sealed report you downloaded still carries its own salt).
