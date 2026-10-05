# Changelog

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
