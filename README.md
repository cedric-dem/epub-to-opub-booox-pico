# epub-to-opub-booox-pico

Convert EPUB e-books into the **OPUB** format used by the Boox Pico e-reader (480×800 screen).

The OPUB layout was reverse-engineered from reference files produced by
"EPUB Tools e-booka.online". It is a FlatBuffers-based container identified by the
`BFPE` magic number. `main.py` builds these files directly from an EPUB without any
external converter.

## What it does

For each EPUB, the converter:

- reads the OPF package (metadata, spine, cover) and the table of contents
- parses each XHTML chapter together with its CSS and computes styles for paragraphs,
  headings, inline text and links (margins, indents, alignment, font size, line height,
  colour, italics)
- splits the text into records of up to 200 bytes of UTF-8
- converts images to 8-bit grayscale with a per-image gamma correction (predicted from the
  image histogram to approximate the reference tool, whose exact rule is unknown) and makes
  1-bit Atkinson-dithered cover thumbnails
  (256×384, 176×264, 128×192)
- writes metadata (`bookId`, `md5sum`, ISBN, creator, publisher, title, language)
- adds a default "external cover" image set from `assets/external_cover.bin`

## Requirements

- Python 3.8+
- Dependencies from `requirements.txt`:
  - `flatbuffers>=24.3.25`
  - `Pillow>=10.0.0`

```powershell
pip install -r requirements.txt
```

## Usage

1. Put your `.epub` files in `sample_library/epub/`.
2. Run:

   ```powershell
   python main.py
   ```

3. The converted books are written to `sample_library/recreated_opub/`, with the same
   file names and an `.opub` extension.

To use other folders, change `SRC_DIR` and `DST_DIR` at the top of `main.py`.

## Project layout

```
main.py                          converter (single script)
compare_results.py               byte-for-byte check of recreated_opub against opub
requirements.txt                 Python dependencies
assets/external_cover.bin        zlib-compressed default cover images (required)
sample_library/
  epub/                          input EPUBs
  opub/                          reference OPUBs made by EPUB Tools (for comparison)
  recreated_opub/                OPUBs made by this script
full_library/                    larger personal library (git-ignored)
```

## OPUB file structure

```
0x000  root table (magic, version, file size, spans to the index sections), padded to 0x100
       chapters: for each spine item, a block (records, paragraph table, record index)
                 followed by a chapter header buffer
       table of contents entries              -> index section B
       computed styles                        -> index section D
       images (8-bit gray / 1-bit dithered)   -> index section E
       metadata key/value table               -> section A
       (chapter, paragraph) list              -> section F
       chapter header index                   -> section C
```

## Comparing with the reference output

`sample_library/opub/` holds OPUBs made by the original tool from the same EPUBs. To check
the converter's output against them, run:

```powershell
python compare_results.py
```

For each file in `sample_library/opub/`, it prints whether the file of the same name in
`sample_library/recreated_opub/` is `IDENTICAL` (byte for byte), `DIFFERENT` or `MISSING`,
then a summary line. It exits with code 0 only if every file is identical.
