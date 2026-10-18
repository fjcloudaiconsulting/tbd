# Changelog

## [0.298.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.297.0...v0.298.0) (2026-10-18)


### Features

* **agent:** delete agent proposals 48 hours after they are staged (TBD-640) ([#956](https://github.com/fjcloudaiconsulting/tbd/issues/956)) ([2ed220f](https://github.com/fjcloudaiconsulting/tbd/commit/2ed220f4e0c3631d49d63120e3c2e55db274e153))
* **ai:** daily OpenRouter model catalog for platform AI, landing dark (TBD-631) ([#949](https://github.com/fjcloudaiconsulting/tbd/issues/949)) ([5015fa3](https://github.com/fjcloudaiconsulting/tbd/commit/5015fa32c814eef72ac5a4b17c5848b7c4308395))
* **ai:** one AI on/off switch per org for own-key AI, platform AI and connected apps (TBD-627) ([#950](https://github.com/fjcloudaiconsulting/tbd/issues/950)) ([1cf9c4c](https://github.com/fjcloudaiconsulting/tbd/commit/1cf9c4c96dfb78e99133be9d705336f2b0f69efb))
* **ai:** publish AI Terms and say where AI data goes (TBD-592) ([#957](https://github.com/fjcloudaiconsulting/tbd/issues/957)) ([6bd2948](https://github.com/fjcloudaiconsulting/tbd/commit/6bd29489d68dbdb53f7bec1fbf6cbb9412fb0039))
* **ai:** strip IBANs, card numbers, emails and phone numbers before anything reaches an AI (TBD-628) ([#953](https://github.com/fjcloudaiconsulting/tbd/issues/953)) ([88d7057](https://github.com/fjcloudaiconsulting/tbd/commit/88d70577a2d2c7820ba1ee2e27fb74b372c7b3f3))
* **oauth:** accept client ID metadata documents so Claude can connect without registering (TBD-634) ([#955](https://github.com/fjcloudaiconsulting/tbd/issues/955)) ([a3675f6](https://github.com/fjcloudaiconsulting/tbd/commit/a3675f679e20840f406a943ce22c270d3c7f4f3c))
* **settings:** org admins list and revoke members' agent tokens (TBD-619) ([#942](https://github.com/fjcloudaiconsulting/tbd/issues/942)) ([f7d5924](https://github.com/fjcloudaiconsulting/tbd/commit/f7d5924df49f582c1a9194462ce4a3938ed60c45))


### Bug Fixes

* **ai:** require an interactive session for AI provider writes (TBD-626) ([#945](https://github.com/fjcloudaiconsulting/tbd/issues/945)) ([d28b5bc](https://github.com/fjcloudaiconsulting/tbd/commit/d28b5bc3e33adf16784929bea605da4c54a5480a))
* **ai:** show every Anthropic model and stop offering claude-sonnet-4-7 (TBD-629) ([#948](https://github.com/fjcloudaiconsulting/tbd/issues/948)) ([51a8eb8](https://github.com/fjcloudaiconsulting/tbd/commit/51a8eb8aa5c764645a35f1c4d31f68379ff30bde))
* **auth:** never render a page as the shared cookie's identity on the server (TBD-624) ([#946](https://github.com/fjcloudaiconsulting/tbd/issues/946)) ([52af5f3](https://github.com/fjcloudaiconsulting/tbd/commit/52af5f3fe922ca225075f9f72ebdad83a4cb0407))
* **auth:** pin each tab's identity so a refresh cannot swap it to another user (TBD-623) ([#944](https://github.com/fjcloudaiconsulting/tbd/issues/944)) ([c156c44](https://github.com/fjcloudaiconsulting/tbd/commit/c156c4479e632e8c2de8de143341fa9dc8721973))
* **auth:** truncate passwords to bcrypt's 72 bytes so bcrypt 5 cannot 500 (TBD-635) ([#951](https://github.com/fjcloudaiconsulting/tbd/issues/951)) ([fe61092](https://github.com/fjcloudaiconsulting/tbd/commit/fe61092c022c4c123e76251463ee67248499eb6d))
* **broadcast:** refuse resuming a draft and keep resumes under the retry cap (TBD-637) ([#954](https://github.com/fjcloudaiconsulting/tbd/issues/954)) ([cb19c69](https://github.com/fjcloudaiconsulting/tbd/commit/cb19c6907cc7df589d7f4f13766e34ed4abf33ff))
* **deps:** repair the frontend lockfile after [#908](https://github.com/fjcloudaiconsulting/tbd/issues/908) and [#930](https://github.com/fjcloudaiconsulting/tbd/issues/930) (TBD-641) ([#963](https://github.com/fjcloudaiconsulting/tbd/issues/963)) ([e7bd218](https://github.com/fjcloudaiconsulting/tbd/commit/e7bd218d775cff4f8560674b127e160c9103a0ba))
* **deps:** update npm non-major ([#908](https://github.com/fjcloudaiconsulting/tbd/issues/908)) ([5597fd3](https://github.com/fjcloudaiconsulting/tbd/commit/5597fd3f49f0a4ab4d7944fe3a2efb9c7367122f))

## [0.297.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.296.1...v0.297.0) (2026-10-15)


### Features

* **ai:** hard input limits on assistant chat per message, turn and tool result (TBD-613) ([#923](https://github.com/fjcloudaiconsulting/tbd/issues/923)) ([44e9c87](https://github.com/fjcloudaiconsulting/tbd/commit/44e9c87483cabff80c3eb7c2da1af90d344d1190))


### Bug Fixes

* **ai:** price the current Claude, OpenAI and Gemini models instead of the fallback rate (TBD-618) ([#907](https://github.com/fjcloudaiconsulting/tbd/issues/907)) ([360e070](https://github.com/fjcloudaiconsulting/tbd/commit/360e0703fa8a120e45e33b928af51dadbaa8bc72))
* **legal:** name Hetzner as host, AWS for backups and Grafana Labs for monitoring (INFRA-105) ([#927](https://github.com/fjcloudaiconsulting/tbd/issues/927)) ([76655fa](https://github.com/fjcloudaiconsulting/tbd/commit/76655fa15f0eb383e9af789c73d4c9d7e8afb9c4))

## [0.296.1](https://github.com/fjcloudaiconsulting/tbd/compare/v0.296.0...v0.296.1) (2026-10-13)


### Bug Fixes

* **ai:** show Agent access only to orgs entitled to ai.agent (TBD-622) ([#921](https://github.com/fjcloudaiconsulting/tbd/issues/921)) ([e95c942](https://github.com/fjcloudaiconsulting/tbd/commit/e95c942da3a34790c943b22e39f621fc4fe01413))

## [0.296.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.295.0...v0.296.0) (2026-10-12)


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
