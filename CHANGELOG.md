# Changelog

## [0.303.1](https://github.com/fjcloudaiconsulting/tbd/compare/v0.303.0...v0.303.1) (2026-10-24)


### Bug Fixes

* **auth:** opt-in email MFA fallback with a reset lock, and bound audit growth (BE-03, BE-10) ([#1019](https://github.com/fjcloudaiconsulting/tbd/issues/1019)) ([a131686](https://github.com/fjcloudaiconsulting/tbd/commit/a131686a2d8724ceea079e2ab3e2b316b2690c2f))
* **deps:** update dependency react-resizable to v4 ([#941](https://github.com/fjcloudaiconsulting/tbd/issues/941)) ([effc909](https://github.com/fjcloudaiconsulting/tbd/commit/effc9098a32f630d6e1770a6793523d58a214474))
* **infra:** public-only post-deploy smoke and FORWARDED_ALLOW_IPS for the proxy trust list (INF-09, INF-01, INF-02) ([#1021](https://github.com/fjcloudaiconsulting/tbd/issues/1021)) ([1fe8ada](https://github.com/fjcloudaiconsulting/tbd/commit/1fe8adaf1cf21804f2c72b7d39800857c6efa7ac))
* **security:** billing entitlements, default org names, email addresses and the tag dictionary (BE-05, DB-03, DB-01, BE-18) ([#1020](https://github.com/fjcloudaiconsulting/tbd/issues/1020)) ([9c6f814](https://github.com/fjcloudaiconsulting/tbd/commit/9c6f8148c955aa31b6b9c9865528ad8800416d6c))
* **security:** bound AI provider responses and the CSP sink, tighten admin and webhook edges ([#1008](https://github.com/fjcloudaiconsulting/tbd/issues/1008)) ([7f47868](https://github.com/fjcloudaiconsulting/tbd/commit/7f4786814504caa13df734ceedaad5a0713d567d))
* **security:** bound request inputs across imports, reports, loans, plans and search ([#1006](https://github.com/fjcloudaiconsulting/tbd/issues/1006)) ([2f36594](https://github.com/fjcloudaiconsulting/tbd/commit/2f36594975d9777bf56f1800d84f774b7e078142))
* **security:** harden CSV export, one-time token URLs and apex CSP, add security.txt ([#1004](https://github.com/fjcloudaiconsulting/tbd/issues/1004)) ([47b8e9f](https://github.com/fjcloudaiconsulting/tbd/commit/47b8e9f0e175399e940fac893b96926d83d0ba57))
* **security:** harden sign-in limits, session revocation, MFA changes and Google SSO ([#1009](https://github.com/fjcloudaiconsulting/tbd/issues/1009)) ([8b9c95a](https://github.com/fjcloudaiconsulting/tbd/commit/8b9c95a3ece9d89277928d26104ea88e1bb69c43))
* **security:** tighten tenant billing, invitation, account and settings logic ([#1005](https://github.com/fjcloudaiconsulting/tbd/issues/1005)) ([772ab8e](https://github.com/fjcloudaiconsulting/tbd/commit/772ab8e3973fbc6ca2e9b307ab6719b407e24319))

## [0.303.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.302.0...v0.303.0) (2026-10-23)


### Features

* **ai:** connect OpenRouter from AI providers in one click (TBD-632) ([#993](https://github.com/fjcloudaiconsulting/tbd/issues/993)) ([0139776](https://github.com/fjcloudaiconsulting/tbd/commit/0139776ceb1279a8e2ba5b8220b29d46914df2a8))
* **ai:** connect OpenRouter with OAuth PKCE (TBD-632) ([#989](https://github.com/fjcloudaiconsulting/tbd/issues/989)) ([fac62b3](https://github.com/fjcloudaiconsulting/tbd/commit/fac62b3b99972c79da450803f261e49c78bc97bf))
* **ai:** list the models a saved key can use, refuse routing to unlisted models (TBD-608) ([#984](https://github.com/fjcloudaiconsulting/tbd/issues/984)) ([1df16e4](https://github.com/fjcloudaiconsulting/tbd/commit/1df16e4fdc90a9889b5edffe954974fadc74fe06))
* **ai:** pick the routing model from the key's model list (TBD-608) ([#987](https://github.com/fjcloudaiconsulting/tbd/issues/987)) ([81a40c9](https://github.com/fjcloudaiconsulting/tbd/commit/81a40c9712ae56a6e6f746270db4f4c8cc1d5a06))


### Bug Fixes

* **auth:** Google sign-in discards the password of an unverified account (TBD-651) ([#994](https://github.com/fjcloudaiconsulting/tbd/issues/994)) ([5b203bc](https://github.com/fjcloudaiconsulting/tbd/commit/5b203bcea7705831cd732a7a7402982ef4a98c93))
* **deps:** bump Pillow to 13.0.0 and certifi to 2026.10.19 (TBD-652) ([#995](https://github.com/fjcloudaiconsulting/tbd/issues/995)) ([bfe2abf](https://github.com/fjcloudaiconsulting/tbd/commit/bfe2abfdfa5e4eb5bc5c8b3b79b1680d5530aea1))

## [0.302.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.301.0...v0.302.0) (2026-10-21)


### Features

* **ai:** platform AI money-safety alarms: daily OpenRouter reconciliation, spend spikes, estimated-cost flag (TBD-633) ([#979](https://github.com/fjcloudaiconsulting/tbd/issues/979)) ([46b5e3a](https://github.com/fjcloudaiconsulting/tbd/commit/46b5e3a0edada6f80325003a8cb47ebafcf94fe2))


### Bug Fixes

* **observability:** every backend process reports a liveness metric, and the MCP server sends OTel (TBD-649) ([#982](https://github.com/fjcloudaiconsulting/tbd/issues/982)) ([2846c0c](https://github.com/fjcloudaiconsulting/tbd/commit/2846c0cdb13c41338e3cb92b4feec9eb6fec4743))

## [0.301.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.300.0...v0.301.0) (2026-10-19)


### Features

* **ai:** platform AI goes OpenRouter only, driven by the model catalog (TBD-586) ([#975](https://github.com/fjcloudaiconsulting/tbd/issues/975)) ([9cdaae5](https://github.com/fjcloudaiconsulting/tbd/commit/9cdaae5e3cc5b27a2e2c2b8a386a39a96952f976))

## [0.300.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.299.1...v0.300.0) (2026-10-19)


### Features

* **frontend:** show the running release in the app footer (TBD-646) ([#972](https://github.com/fjcloudaiconsulting/tbd/issues/972)) ([b6bc947](https://github.com/fjcloudaiconsulting/tbd/commit/b6bc9472a04b983d0c13376230f569a6c2196cab))
* **reports:** dragging a widget rearranges the others, as on the dashboard (TBD-647) ([#973](https://github.com/fjcloudaiconsulting/tbd/issues/973)) ([1fc6b7e](https://github.com/fjcloudaiconsulting/tbd/commit/1fc6b7e805561dc39bd210e5016d0bc929fadb80))

## [0.299.1](https://github.com/fjcloudaiconsulting/tbd/compare/v0.299.0...v0.299.1) (2026-10-18)


### Bug Fixes

* **deps:** revert react-grid-layout to v1; v2 made dashboard drag stick (TBD-644) ([#970](https://github.com/fjcloudaiconsulting/tbd/issues/970)) ([f4e9dc9](https://github.com/fjcloudaiconsulting/tbd/commit/f4e9dc9188d96539a42d9d8b3c06108a8f178094))

## [0.299.0](https://github.com/fjcloudaiconsulting/tbd/compare/v0.298.0...v0.299.0) (2026-10-18)


### Features

* **ai:** meter AI spend in micro-USD and settle on the provider's reported cost (TBD-630) ([#965](https://github.com/fjcloudaiconsulting/tbd/issues/965)) ([9676f04](https://github.com/fjcloudaiconsulting/tbd/commit/9676f045cd44ef4b6192b650ebeac0a73ff41c26))


### Bug Fixes

* **deps:** update dependency react-grid-layout to v2 ([#940](https://github.com/fjcloudaiconsulting/tbd/issues/940)) ([ca47216](https://github.com/fjcloudaiconsulting/tbd/commit/ca472165959170c9dccbdef2dfdb2616ff5e9190))

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
