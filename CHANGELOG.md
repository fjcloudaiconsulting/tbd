# Changelog

## [0.296.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.295.0...v0.296.0) (2026-10-10)


### Features

* **ai:** assistant chat, agent access and review screens (TBD-581) ([#906](https://github.com/fjcloudaiconsulting/tbd/issues/906)) ([d6d551f](https://github.com/fjcloudaiconsulting/tbd/commit/d6d551f6a47bfbd1880cb306475c4c00201159b9))
* **oauth:** keep the OAuth server off unless MCP_OAUTH_ENABLED is set (TBD-587) ([#905](https://github.com/fjcloudaiconsulting/tbd/issues/905)) ([8890ead](https://github.com/fjcloudaiconsulting/tbd/commit/8890ead44b6a5d39ddd997ac2fb66cffb4886a5b))

## [0.295.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.294.0...v0.295.0) (2026-10-09)


### Features

* **oauth:** MCP OAuth 2.1 authorization server (TBD-587) ([#898](https://github.com/fjcloudaiconsulting/tbd/issues/898)) ([6771f9e](https://github.com/fjcloudaiconsulting/tbd/commit/6771f9ee7672124c8f33ded1dbccca18b8f16a5b))
* **telemetry:** own allowlisted spans and native HTTP metrics (INFRA-105) ([#897](https://github.com/fjcloudaiconsulting/tbd/issues/897)) ([4289b8d](https://github.com/fjcloudaiconsulting/tbd/commit/4289b8d92ed20e02ba5b5268a9a10facc0fe5c46))


### Bug Fixes

* **telemetry:** give the OAuth client purge its own job span (TBD-587) ([#903](https://github.com/fjcloudaiconsulting/tbd/issues/903)) ([2ba94cc](https://github.com/fjcloudaiconsulting/tbd/commit/2ba94cce0799ba098044abd41a4049241d2974cb))

## [0.294.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.293.1...v0.294.0) (2026-10-09)


### Features

* **agent:** revert an executed agent write (TBD-589) ([#887](https://github.com/fjcloudaiconsulting/tbd/issues/887)) ([dfd127b](https://github.com/fjcloudaiconsulting/tbd/commit/dfd127b2ca72a5d3db93098225528b8051d51f2d))
* **ai:** platform AI control plane, shipped dark (TBD-586) ([#894](https://github.com/fjcloudaiconsulting/tbd/issues/894)) ([28d27ec](https://github.com/fjcloudaiconsulting/tbd/commit/28d27ecc627e65f2e68a984c975a2553d791504f))
* **ai:** platform AI dispatch kernel, shipped dark (TBD-586) ([#889](https://github.com/fjcloudaiconsulting/tbd/issues/889)) ([03d92e7](https://github.com/fjcloudaiconsulting/tbd/commit/03d92e74b4fdbc5ef2c50b6667844a2562ab4521))


### Bug Fixes

* **db:** release the request DB session before background tasks run (INFRA-128) ([#896](https://github.com/fjcloudaiconsulting/tbd/issues/896)) ([0043858](https://github.com/fjcloudaiconsulting/tbd/commit/0043858b71abfdf896c5178ec9a592c195819d70))
* **deps:** update npm non-major ([#886](https://github.com/fjcloudaiconsulting/tbd/issues/886)) ([5370fb8](https://github.com/fjcloudaiconsulting/tbd/commit/5370fb8746a6477bd6b9e0c4e9e2b178af7b95fd))
* **migrations:** converge prod and fresh schemas on timestamp defaults and the categories org_id index (INFRA-129) ([#893](https://github.com/fjcloudaiconsulting/tbd/issues/893)) ([89113f8](https://github.com/fjcloudaiconsulting/tbd/commit/89113f8eebc2c3ccaac8c35ca6a0d4813511542b))

## [0.293.1](https://github.com/fjcloudaiconsulting/tbd/compare/v0.293.0...v0.293.1) (2026-10-07)


### Bug Fixes

* **auth:** probe the session store before the MFA code check (INFRA-132) ([#882](https://github.com/fjcloudaiconsulting/tbd/issues/882)) ([b110e82](https://github.com/fjcloudaiconsulting/tbd/commit/b110e8231c4738401612b1f7c3fc6d57076beda1))

## [0.293.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.292.2...v0.293.0) (2026-10-05)


### Features

* **rate-limit:** rate limits move to MySQL (INFRA-121) ([#875](https://github.com/fjcloudaiconsulting/tbd/issues/875)) ([9a4da98](https://github.com/fjcloudaiconsulting/tbd/commit/9a4da98951e55580b0ced4ef6ed4809c91f84bda))

## [0.292.2](https://github.com/fjcloudaiconsulting/tbd/compare/v0.292.1...v0.292.2) (2026-10-05)


### Bug Fixes

* **legal:** privacy and terms name AWS Frankfurt as the host (INFRA-112) ([#873](https://github.com/fjcloudaiconsulting/tbd/issues/873)) ([cf1e79d](https://github.com/fjcloudaiconsulting/tbd/commit/cf1e79d89a9858fbaa1d4eddec99e87316ab1e0c))

## [0.292.1](https://github.com/fjcloudaiconsulting/tbd/compare/v0.292.0...v0.292.1) (2026-10-05)


### Bug Fixes

* **migrations:** fresh MySQL 8.4 upgrade past 050 with SQLAlchemy &gt;= 2.0.42 (INFRA-109) ([#869](https://github.com/fjcloudaiconsulting/tbd/issues/869)) ([d165abd](https://github.com/fjcloudaiconsulting/tbd/commit/d165abd2dfb7bdb3385ab6f4d87e2f556e7e648c))

## [0.292.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.291.2...v0.292.0) (2026-10-05)


### Features

* **apex:** deploy the apex static export to a Cloudflare Worker preview (INFRA-60) ([#837](https://github.com/fjcloudaiconsulting/tbd/issues/837)) ([f31ed47](https://github.com/fjcloudaiconsulting/tbd/commit/f31ed47e4adb26fa928de3d284c098b1551f9ecb))

## [0.291.2](https://github.com/fjcloudaiconsulting/tbd/compare/v0.291.1...v0.291.2) (2026-10-05)


### Bug Fixes

* **backend:** security update for FastAPI and starlette (INFRA-124) ([#859](https://github.com/fjcloudaiconsulting/tbd/issues/859)) ([81fdc31](https://github.com/fjcloudaiconsulting/tbd/commit/81fdc31ce65a2a14bfc554d65cfc0d0faa9595f6))
* **frontend:** security update for next 16.3.8 (INFRA-124) ([#858](https://github.com/fjcloudaiconsulting/tbd/issues/858)) ([883e307](https://github.com/fjcloudaiconsulting/tbd/commit/883e3075231285bcc5ef0ad2b95ed50464f7b0d7))

## [0.291.1](https://github.com/fjcloudaiconsulting/tbd/compare/v0.291.0...v0.291.1) (2026-10-04)


### Bug Fixes

* **ci:** await only the push run on main for a release commit (INFRA-96) ([#830](https://github.com/fjcloudaiconsulting/tbd/issues/830)) ([e256f89](https://github.com/fjcloudaiconsulting/tbd/commit/e256f897eeaf82bae26a508c348417244f7a193d))
* **logging:** keep query strings out of access logs (INFRA-110) ([#836](https://github.com/fjcloudaiconsulting/tbd/issues/836)) ([aeb20e2](https://github.com/fjcloudaiconsulting/tbd/commit/aeb20e2a2ea8f91e90669ce285ff66172c0ad14b))
* **scripts:** keep credentials out of curl argv (INFRA-95) ([#831](https://github.com/fjcloudaiconsulting/tbd/issues/831)) ([6ff2807](https://github.com/fjcloudaiconsulting/tbd/commit/6ff2807259db5daac9a66b114a2d6fcf734392e9))

## [0.291.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.290.0...v0.291.0) (2026-10-04)


### Features

* **ci:** release with release-please and the shared workflows (INFRA-42) ([#823](https://github.com/fjcloudaiconsulting/tbd/issues/823)) ([ef439f0](https://github.com/fjcloudaiconsulting/tbd/commit/ef439f0374de47f85b2b661eb7a3740b8419ec6e))


### Bug Fixes

* **backend:** client IP behind Cloudflare and a migration lock (INFRA-83) ([#824](https://github.com/fjcloudaiconsulting/tbd/issues/824)) ([1a4080a](https://github.com/fjcloudaiconsulting/tbd/commit/1a4080a5dbe8c5d8af2b5910b626f8f6adccf665))

## [0.290.0](https://github.com/fjcloudaiconsulting/tbd/releases/tag/v0.290.0) (2026-10-03)

Releases up to and including v0.290.0 were cut by semantic-release. Their notes are on the
[GitHub Releases page](https://github.com/fjcloudaiconsulting/tbd/releases). release-please writes every later entry above this one.
