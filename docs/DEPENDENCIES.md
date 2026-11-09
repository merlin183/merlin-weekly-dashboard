# Dependency inventory

This snapshot records package versions and license metadata for the committed lockfiles. It includes transitive and optional platform packages; a particular installation may install only a subset. Metadata is a reference, not a complete legal review. Preserve each package's license and notice files when redistributing it; the project's MIT license does not replace them.

Python entries come from the installed Python 3.12 distributions matching [requirements.lock](../requirements.lock). Node entries come from [package-lock.json](../package-lock.json) and [cf/package-lock.json](../cf/package-lock.json). Update this inventory when those locks change. License values retain their published metadata wording, including expressions and exceptions.

Container base images, bundled system/native libraries, and browser CDN assets are outside these package tables. See [third-party notices](../THIRD_PARTY_NOTICES.md).

## Python — 57 distributions

| Package | Version | License metadata |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.162.0 | MIT |
| google-api-core | 2.43.2 | Apache 2.0 |
| google-api-python-client | 2.201.0 | Apache 2.0 |
| google-auth | 2.62.0 | Apache 2.0 |
| google-auth-httplib2 | 0.4.4 | Apache 2.0 |
| google-auth-oauthlib | 1.5.0 | Apache 2.0 |
| google-cloud-bigquery | 3.47.0 | Apache 2.0 |
| google-cloud-core | 2.8.0 | Apache 2.0 |
| google-crc32c | 1.9.0 | Apache-2.0 |
| google-resumable-media | 2.11.0 | Apache 2.0 |
| googleapis-common-protos | 1.76.0 | Apache-2.0 |
| grpcio | 1.85.0 | Apache-2.0 |
| grpcio-status | 1.85.0 | Apache-2.0 |
| gspread | 6.2.1 | License :: OSI Approved :: MIT License |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httplib2 | 0.32.1 | MIT |
| httptools | 0.9.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| oauthlib | 4.0.0 | BSD-3-Clause |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| proto-plus | 1.29.0 | Apache 2.0 |
| protobuf | 7.36.2 | 3-Clause BSD License |
| psycopg2-binary | 2.9.13 | LGPL with exceptions |
| pyasn1 | 0.6.5 | BSD-2-Clause |
| pyasn1_modules | 0.4.2 | BSD |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.14.2 | MIT |
| pydantic_core | 2.50.2 | MIT |
| PyJWT | 2.15.1 | MIT |
| pyparsing | 3.3.3 | MIT |
| python-dateutil | 2.9.0.post0 | Dual License |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| requests-oauthlib | 2.0.0 | ISC |
| six | 1.17.0 | MIT |
| starlette | 1.10.0 | BSD-3-Clause |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.6 | Apache-2.0 |
| uritemplate | 4.2.0 | BSD 3-Clause OR Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| watchfiles | 1.3.0 | MIT |
| websockets | 17.3 | BSD-3-Clause |

## Node — Frontend build (73 entries)

| Package | Version | License metadata |
| --- | --- | --- |
| @alloc/quick-lru | 5.3.0 | MIT |
| @jridgewell/gen-mapping | 0.3.13 | MIT |
| @jridgewell/resolve-uri | 3.1.2 | MIT |
| @jridgewell/sourcemap-codec | 1.6.0 | MIT |
| @jridgewell/trace-mapping | 0.3.31 | MIT |
| @nodelib/fs.scandir | 2.1.5 | MIT |
| @nodelib/fs.stat | 2.0.5 | MIT |
| @nodelib/fs.walk | 1.2.8 | MIT |
| any-promise | 1.3.0 | MIT |
| anymatch | 3.1.3 | ISC |
| arg | 5.0.2 | MIT |
| binary-extensions | 2.3.0 | MIT |
| braces | 3.0.3 | MIT |
| camelcase-css | 2.0.1 | MIT |
| chokidar | 3.6.0 | MIT |
| commander | 4.1.1 | MIT |
| didyoumean | 1.2.2 | Apache-2.0 |
| dlv | 1.1.3 | MIT |
| es-errors | 1.3.0 | MIT |
| fast-glob | 3.3.3 | MIT |
| fastq | 1.20.3 | ISC |
| fdir | 6.5.0 | MIT |
| fill-range | 7.1.1 | MIT |
| fsevents | 2.3.3 | MIT |
| function-bind | 1.1.2 | MIT |
| glob-parent | 5.1.2 | ISC |
| glob-parent | 5.1.2 | ISC |
| glob-parent | 6.0.2 | ISC |
| hasown | 2.0.4 | MIT |
| is-binary-path | 2.1.0 | MIT |
| is-core-module | 2.18.0 | MIT |
| is-extglob | 2.1.1 | MIT |
| is-glob | 4.0.3 | MIT |
| is-number | 7.0.0 | MIT |
| jiti | 1.21.7 | MIT |
| lilconfig | 3.1.4 | MIT |
| lines-and-columns | 1.2.4 | MIT |
| merge2 | 1.4.1 | MIT |
| micromatch | 4.0.8 | MIT |
| mz | 2.7.0 | MIT |
| nanoid | 3.3.20 | MIT |
| normalize-path | 3.0.0 | MIT |
| object-assign | 4.1.1 | MIT |
| object-hash | 3.0.0 | MIT |
| path-parse | 1.0.7 | MIT |
| picocolors | 1.1.1 | ISC |
| picomatch | 2.3.2 | MIT |
| picomatch | 4.0.7 | MIT |
| pirates | 4.0.7 | MIT |
| postcss | 8.5.32 | MIT |
| postcss-import | 15.1.0 | MIT |
| postcss-js | 4.1.0 | MIT |
| postcss-load-config | 4.0.2 | MIT |
| postcss-nested | 6.2.0 | MIT |
| postcss-selector-parser | 6.1.6 | MIT |
| postcss-value-parser | 4.2.0 | MIT |
| queue-microtask | 1.2.3 | MIT |
| read-cache | 1.0.2 | MIT |
| readdirp | 3.6.0 | MIT |
| resolve | 1.22.14 | MIT |
| reusify | 1.1.0 | MIT |
| run-parallel | 1.2.0 | MIT |
| source-map-js | 1.2.2 | BSD-3-Clause |
| sucrase | 3.35.1 | MIT |
| supports-preserve-symlinks-flag | 1.0.0 | MIT |
| tailwindcss | 3.4.17 | MIT |
| thenify | 3.3.1 | MIT |
| thenify-all | 1.6.0 | MIT |
| tinyglobby | 0.2.17 | MIT |
| to-regex-range | 5.0.1 | MIT |
| ts-interface-checker | 0.1.13 | Apache-2.0 |
| util-deprecate | 1.0.2 | MIT |
| yaml | 2.9.1 | ISC |

## Node — Optional Cloudflare wrapper (114 entries)

| Package | Version | License metadata |
| --- | --- | --- |
| @cloudflare/containers | 0.3.7 | MIT OR Apache-2.0 |
| @cloudflare/kv-asset-handler | 0.5.0 | MIT OR Apache-2.0 |
| @cloudflare/unenv-preset | 2.16.2 | MIT OR Apache-2.0 |
| @cloudflare/workerd-darwin-64 | 1.20260926.1 | Apache-2.0 |
| @cloudflare/workerd-darwin-arm64 | 1.20260926.1 | Apache-2.0 |
| @cloudflare/workerd-linux-64 | 1.20260926.1 | Apache-2.0 |
| @cloudflare/workerd-linux-arm64 | 1.20260926.1 | Apache-2.0 |
| @cloudflare/workerd-windows-64 | 1.20260926.1 | Apache-2.0 |
| @cloudflare/workers-types | 5.20260929.1 | MIT OR Apache-2.0 |
| @cspotcode/source-map-support | 0.8.1 | MIT |
| @emnapi/runtime | 1.11.3 | MIT |
| @esbuild/aix-ppc64 | 0.28.1 | MIT |
| @esbuild/android-arm | 0.28.1 | MIT |
| @esbuild/android-arm64 | 0.28.1 | MIT |
| @esbuild/android-x64 | 0.28.1 | MIT |
| @esbuild/darwin-arm64 | 0.28.1 | MIT |
| @esbuild/darwin-x64 | 0.28.1 | MIT |
| @esbuild/freebsd-arm64 | 0.28.1 | MIT |
| @esbuild/freebsd-x64 | 0.28.1 | MIT |
| @esbuild/linux-arm | 0.28.1 | MIT |
| @esbuild/linux-arm64 | 0.28.1 | MIT |
| @esbuild/linux-ia32 | 0.28.1 | MIT |
| @esbuild/linux-loong64 | 0.28.1 | MIT |
| @esbuild/linux-mips64el | 0.28.1 | MIT |
| @esbuild/linux-ppc64 | 0.28.1 | MIT |
| @esbuild/linux-riscv64 | 0.28.1 | MIT |
| @esbuild/linux-s390x | 0.28.1 | MIT |
| @esbuild/linux-x64 | 0.28.1 | MIT |
| @esbuild/netbsd-arm64 | 0.28.1 | MIT |
| @esbuild/netbsd-x64 | 0.28.1 | MIT |
| @esbuild/openbsd-arm64 | 0.28.1 | MIT |
| @esbuild/openbsd-x64 | 0.28.1 | MIT |
| @esbuild/openharmony-arm64 | 0.28.1 | MIT |
| @esbuild/sunos-x64 | 0.28.1 | MIT |
| @esbuild/win32-arm64 | 0.28.1 | MIT |
| @esbuild/win32-ia32 | 0.28.1 | MIT |
| @esbuild/win32-x64 | 0.28.1 | MIT |
| @img/colour | 1.1.0 | MIT |
| @img/sharp-darwin-arm64 | 0.35.4 | Apache-2.0 |
| @img/sharp-darwin-x64 | 0.35.4 | Apache-2.0 |
| @img/sharp-freebsd-wasm32 | 0.35.4 | Apache-2.0 |
| @img/sharp-libvips-darwin-arm64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-darwin-x64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-ppc64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-riscv64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-s390x | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-x64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-arm64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-x64 | 1.3.3 | LGPL-3.0-or-later |
| @img/sharp-linux-arm | 0.35.4 | Apache-2.0 |
| @img/sharp-linux-arm64 | 0.35.4 | Apache-2.0 |
| @img/sharp-linux-ppc64 | 0.35.4 | Apache-2.0 |
| @img/sharp-linux-riscv64 | 0.35.4 | Apache-2.0 |
| @img/sharp-linux-s390x | 0.35.4 | Apache-2.0 |
| @img/sharp-linux-x64 | 0.35.4 | Apache-2.0 |
| @img/sharp-linuxmusl-arm64 | 0.35.4 | Apache-2.0 |
| @img/sharp-linuxmusl-x64 | 0.35.4 | Apache-2.0 |
| @img/sharp-wasm32 | 0.35.4 | Apache-2.0 AND LGPL-3.0-or-later AND MIT |
| @img/sharp-webcontainers-wasm32 | 0.35.4 | Apache-2.0 |
| @img/sharp-win32-arm64 | 0.35.4 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-ia32 | 0.35.4 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-x64 | 0.35.4 | Apache-2.0 AND LGPL-3.0-or-later |
| @jridgewell/resolve-uri | 3.1.2 | MIT |
| @jridgewell/sourcemap-codec | 1.6.0 | MIT |
| @jridgewell/trace-mapping | 0.3.9 | MIT |
| @poppinss/colors | 4.1.6 | MIT |
| @poppinss/dumper | 0.6.5 | MIT |
| @poppinss/exception | 1.2.3 | MIT |
| @sindresorhus/is | 7.2.0 | MIT |
| @speed-highlight/core | 1.2.24 | CC0-1.0 |
| @typescript/typescript-aix-ppc64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-darwin-arm64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-darwin-x64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-freebsd-arm64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-freebsd-x64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-arm | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-arm64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-loong64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-mips64el | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-ppc64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-riscv64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-s390x | 7.0.2 | Apache-2.0 |
| @typescript/typescript-linux-x64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-netbsd-arm64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-netbsd-x64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-openbsd-arm64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-openbsd-x64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-sunos-x64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-win32-arm64 | 7.0.2 | Apache-2.0 |
| @typescript/typescript-win32-x64 | 7.0.2 | Apache-2.0 |
| blake3-wasm | 2.1.5 | MIT |
| cookie | 1.1.1 | MIT |
| detect-libc | 2.1.2 | Apache-2.0 |
| error-stack-parser-es | 1.0.5 | MIT |
| esbuild | 0.28.1 | MIT |
| fsevents | 2.3.3 | MIT |
| kleur | 4.1.5 | MIT |
| miniflare | 5.20260926.1-alpha | MIT |
| path-to-regexp | 6.3.0 | MIT |
| pathe | 2.0.3 | MIT |
| semver | 7.8.5 | ISC |
| sharp | 0.35.4 | Apache-2.0 |
| supports-color | 10.2.2 | MIT |
| tslib | 2.8.1 | 0BSD |
| typescript | 7.0.2 | Apache-2.0 |
| undici | 7.29.1 | MIT |
| unenv | 2.0.0-rc.24 | MIT |
| workerd | 1.20260926.1 | Apache-2.0 |
| wrangler | 4.144.0 | MIT OR Apache-2.0 |
| ws | 8.21.0 | MIT |
| youch | 4.1.0-beta.10 | MIT |
| youch-core | 0.3.3 | MIT |
