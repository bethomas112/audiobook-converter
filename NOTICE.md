# Third-party code

This project is otherwise original code, but one file is a substantial,
mostly-verbatim port from another open-source project, included here per
the terms of its license.

## `app/pipeline/chapter_aligner.py`

Ported from **achew** (<https://github.com/SirGibblets/achew>),
`backend/app/services/chapter_aligner.py`, at commit `5e8e249` (v1.12.0).
achew is MIT-licensed. The `ChapterAligner` class and its supporting
constants/docstrings are carried over unchanged; only its two small input
types (`BasicChapter`, `DetectedCue`) were reproduced as local dataclasses
in place of the Pydantic models achew imports from its own web-API layer,
which this project doesn't otherwise depend on. See the header comment at
the top of `app/pipeline/chapter_aligner.py` for the itemized diff.

achew's chapter-realignment test suite
(`backend/tests/realignment/realignment_helpers.py` and a subset of its
captured real-book fixtures) was likewise ported into this project's
`tests/unit/test_chapter_aligner.py` / `tests/unit/chapter_aligner_fixtures/`
as a regression baseline for the ported algorithm.

### License

```
MIT License

Copyright (c) 2025 Sir Gibblets

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
