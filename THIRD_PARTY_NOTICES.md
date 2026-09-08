# Third-party notices

## polymarket-tmax-lab

Parts of the settlement-rule parsing design, source-gating model, and associated test cases were adapted from:

- Project: `polymarket-tmax-lab`
- Repository: https://github.com/YoungseokOh/polymarket-tmax-lab
- Author: Youngseok Oh
- Reviewed revision: unavailable in the repository notice/history; no revision hash is asserted.
- Files consulted: `markets/rule_parser.py`, `markets/market_spec.py`, `markets/outcome_schema.py`, `backtest/rolling_origin.py`, and their tests
- License: MIT

MIT License

Copyright (c) 2026

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

## NautilusTrader

The optional `nautilus-eval` extra pins `nautilus-trader==2.0.0rc4` from
[Nautech Systems' NautilusTrader repository](https://github.com/nautechsystems/nautilus_trader).
It is used unmodified and only by the isolated offline conformance challenger
in `src/poly_weather/nautilus_conformance.py`.  It is not a Paper V1 authority,
does not receive credentials, and is not used by a resident runtime.  NautilusTrader
is licensed under the GNU Lesser General Public License v3.0 (LGPL-3.0).

Local distribution check (2026-09-08): the installed rc4 distribution contains
`nautilus_trader-2.0.0rc4.dist-info/licenses/LICENSE` (LGPL v3). That text
incorporates GPL v3 and its sections 3(b) and 4(b) require both texts for the
covered forms of distribution. This repository declares an optional dependency;
this task does not build or distribute a bundled Nautilus artifact. Any future
bundle must review these conditions and supply the applicable license texts and
source/relinking materials before release; the installed LGPL file alone is not
a completed redistribution review.

## Spencer Fletcher market-maker

The design and test-specification review consulted
[`spencerfletcher/market-maker`](https://github.com/spencerfletcher/market-maker)
at revision `68f6c44730dab0772a3601072b0e00ee190f3a4c`, including
`bot/core/durable.py`.  No upstream source code was copied into this project.
Its MIT license is reproduced below for attribution to the reviewed source.

Copyright (c) 2026 Spencer Fletcher

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
