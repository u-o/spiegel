# Der Spiegel archive: theatre coverage

Tools for downloading the print archive of *Der Spiegel* (1947–1989), turning it into text, and finding its theatre coverage. The project is part of theatre-history research at LMU München.

The repository holds **only code**. The data itself, about 4,170 issues and 125 GB, is kept outside the repository. You tell the scripts where it is in a local `config.toml` (see below).

## Layout

```
crawler/     download issues, PDFs and article texts from spiegel.de (needs a Spiegel account)
extract/     PDF → text: dedupe scan pages, OCR, contents-page (TOC) extraction
viewers/     local web viewer: cover + contents page per issue
data/        analysis output (not tracked by git; the corpus itself lives at data_dir)
show-*.py    quick searches over sections, titles and PDF text
spiegel_config.py    finds the corpus folder (see below)
config.example.toml  template for your local config.toml
```

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`extract/` also needs **poppler** (`pdftotext`, `pdfinfo`, `pdfimages`, `pdftoppm`, `pdfseparate`, `pdfunite`) and **tesseract** with German language data. On macOS:

```sh
brew install poppler tesseract tesseract-lang
```

## Pointing the scripts at the data: `config.toml`

The scripts need to know where the corpus is: the folder that contains `spiegel-1965-10.pdf`, `spiegel-1965-10/` and the other issue files. This path differs from machine to machine, so it is kept in a local config file that is **not** tracked by git.

Set it up once:

```sh
cp config.example.toml config.toml
```

Then edit `config.toml`:

```toml
data_dir = "/Volumes/your-drive/spiegel/downloads"   # Windows: "Z:/spiegel/downloads"
```

All scripts get the path through `spiegel_config.py`, which checks these places in order:

1. the environment variable **`SPIEGEL_DATA`**, if it is set: either a path, or a name such as `sample` that selects `sample_dir` from `config.toml`;
2. **`data_dir`** in `config.toml`;
3. `downloads/` inside the repository.

This makes it easy to switch between the full corpus and the sample for a single run. Add the sample to `config.toml`:

```toml
sample_dir = "/path/to/spiegel-sample/downloads"
```

and run:

```sh
SPIEGEL_DATA=sample .venv/bin/python3 extract/extract-toc.py
```

Paths are resolved relative to the repository, so you can run the scripts from any folder. Analysis output always goes to `data/` in the repository.

## Data layout

```
<data_dir>/
  _year-1965.html             year index from spiegel.de
  spiegel-1965-10.pdf         print edition (page scans up to 1993, born-digital from 1994)
  spiegel-1965-10.html        issue overview page from spiegel.de
  spiegel-1965-10/
    pdf-pages/page-N.txt      text of PDF page N (OCR up to 1993, PDF text layer from 1994)
    pagemap.json              scan era only: maps pages after dedupe to the original PDF
    Kultur/ Ausland/ ...      online article texts, one folder per spiegel.de section
```

The full corpus also has `_pdf_orig/`, the original scan PDFs from before dedupe, and `_ocr_hash_cache/`. Neither is needed for analysis.

## Crawler (owner only)

`crawler/` needs two files that are not in the repository:
- `crawler/cookies.txt`: the Spiegel account session, in Netscape format.
- `crawler/user_agent.txt`: the identifying User-Agent. Copy `user_agent.example.txt` to create it.
