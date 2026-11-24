# Teacher web app

A teacher-only web interface on top of the original answer-sheet evaluation pipeline.

1. Teacher signs in (there is no student login and no sign-up page).
2. Teacher uploads the question paper and the model answers.
3. Teacher uploads answer sheets in bulk (many files, or one ZIP).
4. The system detects each student's answers (Q1:, 1., 1) ... in any order, across pages).
5. Each answer is compared with the model answer.
6. Scores are shown per student and per question, and can be downloaded as CSV.

## Run it

```bash
cd "Answer Sheet Evaluation System"
pip install -r config/requirements.txt
python app.py
```

Open http://127.0.0.1:5000. On first run a teacher account is created and its password is printed once in the terminal.
To choose your own, set `TEACHER_USERNAME` and `TEACHER_PASSWORD` before the first run.

More teachers / lost password:

```bash
python manage.py create-teacher <username>
python manage.py reset-password <username>
```

## Input formats

| File | Accepted |
|------|----------|
| Answer key | PDF, TXT or CSV. Number items `Q1:`, `1.` or `1)`. Optional `Answer:` label. CSV columns: `Question Number, Question, Answer, Marks` (your existing `data/QuestionAnswersDataSet.csv` works as is). |
| Question paper | PDF, TXT or CSV (optional). Put "(5 marks)" or "[5]" at the end of a question to set its marks. |
| Answer sheets | Text PDFs, TXT, and (with `pip install easyocr`) scanned PDFs and JPG/PNG photos. Upload several files or a ZIP. |

Name files `S001_Asha_Rao.pdf`, or put `Roll No:` / `Name:` lines at the top of the sheet. You can correct either on the student's page.

## How scoring works

Same three parts as the original prototype, per question:

`marks = max_marks x (w_grammar x spelling + w_keyword x keywords + w_semantic x meaning) / (sum of weights)`

* **Spelling** - share of correctly spelled words (TextBlob's dictionary; words from the question/model answer are never flagged).
* **Keywords** - the key terms from the model answer found in the student's answer, matched on word stems. Keywords are generated automatically and editable per question.
* **Meaning** - cosine similarity to the model answer using `sentence-transformers` (`all-MiniLM-L6-v2`). If that package or its model is not available the app falls back to TF-IDF similarity and says so on the student's page.
* Spelling credit is scaled by relevance, so a well-spelled but unrelated answer earns nothing.
* Weights default to 20 / 50 / 30 and can be changed per exam and per question.

## Things worth knowing

* If the same question number appears twice on a sheet, the first occurrence is scored and the sheet is flagged.
* Percentages use the total of **all** questions in the key, so unanswered questions count as zero.
* Editing the key marks earlier results as out of date; use "Score all sheets again".
* Handwriting: EasyOCR reads clean printing but is unreliable on handwriting. Always spot-check OCR'd sheets (the page tells you when OCR was used).
* Data lives in `instance/` (SQLite database, uploads, secret key). Delete the folder to start fresh.

## Deploying beyond your own computer

The default server only listens on 127.0.0.1. For a school server put it behind HTTPS (nginx/Caddy), set `COOKIE_SECURE=1` and `SECRET_KEY`, and run a single process, e.g.
`gunicorn -w 1 --threads 8 -b 127.0.0.1:8000 app:app` (scoring runs in a background thread inside the process).

## Tests

`python tests/smoke_test.py` runs an end-to-end check (login, upload, scoring, export, per-teacher isolation) against the sample PDFs in `Test/`.

## Command line (original workflow, still works)

`python cli.py --rollno S001 --name "John Doe" --pdf Test/test_perfect_refined.pdf`
