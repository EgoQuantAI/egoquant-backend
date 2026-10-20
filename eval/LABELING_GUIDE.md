# Headline labeling guide

We are measuring how well our tone models read financial headlines. For that we need **human** labels.
Your labels are the ground truth, so please follow these rules exactly.

## The file

`eval/labeling_sheet.csv` has one row per headline:

| Column | Meaning |
|---|---|
| `block` | A, B, C or D. You label two blocks (see the assignment below) |
| `id` | Do not change |
| `headline` | The headline to judge |
| `source` | Where it was published (for reference only) |
| `matched_tickers` | The fund(s) the headline is about, e.g. `TLT` or `VNQ QQQ` |
| `label` | **Your answer:** `positive`, `negative` or `neutral` (lower case) |

## What the labels mean

Imagine **an investor who holds the matched fund**. Is this headline:

- **positive**: likely good news for that investor.
- **negative**: likely bad news for that investor.
- **neutral**: factual, mixed or unclear for that investor.

If a row lists several funds and the news points in different directions for them, use **neutral**.

## Rules

1. **Judge the headline only.** Don't open the link or read the article.
2. **If you are unsure, choose neutral.**
3. **Label alone.** Don't discuss rows with anyone until everyone has finished.
4. **No AI.** Don't use ChatGPT, Claude, FinBERT or any other tool to suggest a label. Your own judgment only.
5. Don't change any column except `label`, and don't add, delete or reorder rows.

## Example

Headline: **"Treasury yields jump"**

- matched `TLT` (long-term Treasury bonds) → **negative**: when yields rise, long bond prices fall.
- matched `BIL` (1–3 month Treasury bills) → **neutral**: very short bills barely move, and new bills pay more.

The same headline can deserve different labels for different funds. Always judge for the matched fund.

## Who labels which blocks

There are four blocks of 30 headlines, and every block is labeled by **two** people independently:

| Person | Blocks |
|---|---|
| person1 | A + B |
| person2 | B + C |
| person3 | C + D |
| person4 | D + A |

## Steps

1. The coordinator runs `python eval/make_labeling_handouts.py`. That creates one Excel file per person,
   `eval/labeling/personN.xlsx`, with that person's 60 headlines in random order, a dropdown for the label,
   and these instructions. The folder is gitignored, so these files are never committed.
2. Each person fills the `label` column (dropdown: positive, negative or neutral) alone, using the rules
   above, and sends the file back to the coordinator.
3. The coordinator puts the four returned files in `eval/labeling/` and runs `python eval/merge_labels.py`.
   It checks that every row has a valid label and that the ids match the sheet, then writes
   `eval/labels_merged.csv` (`id, headline, label_a, label_b, agree`) and prints the agreement rate and
   every disagreement.
4. The merge also creates `eval/labeling/disagreements.xlsx`: one row per disagreement with the two labels (no
   names) and an empty `final` column. The two labelers of each headline agree a final label together, reading
   only the headline; if they are still unsure, `neutral`.
5. Run `python eval/merge_labels.py` again. It writes `eval/headlines_labeled.csv` (agreed rows labeled `agreed`,
   resolved rows `adjudicated`; gitignored, never committed). Then run `python eval/run_eval.py`.
