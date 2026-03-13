# Task: Build a Word Frequency CLI

Build a Python CLI tool that counts word frequencies in text files.

## Requirements

Create a file called `wordfreq.py` that:

1. Accepts one or more file paths as arguments
2. Reads each file and counts word frequencies (case-insensitive)
3. Outputs the top 10 most frequent words with their counts
4. Supports a `--limit N` flag to change the number of results
5. Supports a `--sort` flag with options "freq" (default) or "alpha"
6. Handles errors gracefully (missing files, empty files)
7. Ignores common stop words: the, a, an, is, are, was, were, in, on, at, to, for, of, and, or, but, not

Create a test file called `test_wordfreq.py` with tests for:
- Basic word counting
- Case insensitivity
- Stop word filtering
- Multiple file input
- The --limit flag
- The --sort flag
- Error handling for missing files

Also create a sample text file `sample.txt` with at least 100 words to test with.

## Verification

Run `python3 test_wordfreq.py` — all tests must pass.
Run `python3 wordfreq.py sample.txt` — should output top 10 words.
Run `python3 wordfreq.py sample.txt --limit 5 --sort alpha` — should work.
