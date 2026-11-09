# Third-party notices

The project code is offered under the [MIT License](LICENSE). This does not relicense dependencies, container images, externally loaded scripts, fonts, services, or other third-party material. Their own licenses and notices continue to apply.

## Frontend assets

| Component | Use in this repository | License handling |
| --- | --- | --- |
| Tailwind CSS 3.4.17 | Compiled stylesheet in `static/tailwind.css`; build input in `tailwind/` | MIT; preserve the generated version/license banner and the notice below. |
| Alpine.js 3.14.9 | Browser script referenced by `static/index.html` from jsDelivr | MIT; preserve upstream notices if you download and redistribute it. |

jsDelivr is a delivery service, not the author of these packages. A CDN script is still a third-party dependency even when it is not checked into this repository.

### Tailwind CSS

Copyright (c) Tailwind Labs, Inc.

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

## Installed dependencies

The [dependency inventory](docs/DEPENDENCIES.md) records versions and license metadata for the pinned Python set and both Node dependency trees. Python packages are pinned in [requirements.lock](requirements.lock), installed through [requirements.txt](requirements.txt). The frontend build uses [package-lock.json](package-lock.json); the optional Cloudflare wrapper uses [cf/package-lock.json](cf/package-lock.json). These include packages under licenses other than MIT. Consult each installed distribution's `LICENSE`, `COPYING`, `NOTICE`, and package metadata, including transitive dependencies and bundled native libraries, before redistributing a built image.

Python, PostgreSQL, the base operating-system image, and optional cloud SDKs retain their respective licenses. Installing a dependency or using a hosted service does not change its license or service terms.

## Adaptation notice

This public project was adapted from a private application. Private operational data and configuration are not part of the public distribution. This notice grants no rights to private data, third-party trademarks, or external services.
