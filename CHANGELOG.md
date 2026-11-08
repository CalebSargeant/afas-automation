# CHANGELOG

<!-- version list -->

## v1.5.2 (2026-11-08)

### Bug Fixes

- **readback**: Wait for a page to settle before calling it the end of the list
  ([#33](https://github.com/CalebSargeant/afas-automation/pull/33),
  [`6c7cd50`](https://github.com/CalebSargeant/afas-automation/commit/6c7cd509efb49ae77bacb2ee4482da40e3e9930d))


## v1.5.1 (2026-11-08)

### Bug Fixes

- **owa**: Say what unparsed and stray calendar labels were
  ([#32](https://github.com/CalebSargeant/afas-automation/pull/32),
  [`a6fd1e8`](https://github.com/CalebSargeant/afas-automation/commit/a6fd1e840838c2f85ad2ae49415ca71f13b0f380))

### Documentation

- Describe the label shapes in the week log
  ([#32](https://github.com/CalebSargeant/afas-automation/pull/32),
  [`a6fd1e8`](https://github.com/CalebSargeant/afas-automation/commit/a6fd1e840838c2f85ad2ae49415ca71f13b0f380))

- Outlook refuses the sign-in from some networks
  ([#31](https://github.com/CalebSargeant/afas-automation/pull/31),
  [`e630ff1`](https://github.com/CalebSargeant/afas-automation/commit/e630ff1dedb3faf152c29b6870f6710c396c6a86))


## v1.5.0 (2026-11-08)

### Documentation

- Describe the per-job nodeSelector
  ([#30](https://github.com/CalebSargeant/afas-automation/pull/30),
  [`61e3115`](https://github.com/CalebSargeant/afas-automation/commit/61e311541fc8950791d59a59166ea87ba6371ebc))

### Features

- Keep classify, build and submit going through a refused sign-in
  ([#30](https://github.com/CalebSargeant/afas-automation/pull/30),
  [`61e3115`](https://github.com/CalebSargeant/afas-automation/commit/61e311541fc8950791d59a59166ea87ba6371ebc))

- Survive a refused sign-in, per-job nodeSelector, English Slack text
  ([#30](https://github.com/CalebSargeant/afas-automation/pull/30),
  [`61e3115`](https://github.com/CalebSargeant/afas-automation/commit/61e311541fc8950791d59a59166ea87ba6371ebc))

- **chart**: Let a job set its own nodeSelector
  ([#30](https://github.com/CalebSargeant/afas-automation/pull/30),
  [`61e3115`](https://github.com/CalebSargeant/afas-automation/commit/61e311541fc8950791d59a59166ea87ba6371ebc))

- **slack**: Post every message in English
  ([#30](https://github.com/CalebSargeant/afas-automation/pull/30),
  [`61e3115`](https://github.com/CalebSargeant/afas-automation/commit/61e311541fc8950791d59a59166ea87ba6371ebc))

### Testing

- Assert the English text in the Postgres lifecycle tests
  ([#30](https://github.com/CalebSargeant/afas-automation/pull/30),
  [`61e3115`](https://github.com/CalebSargeant/afas-automation/commit/61e311541fc8950791d59a59166ea87ba6371ebc))


## v1.4.2 (2026-11-07)

### Bug Fixes

- **calendar**: Answer a sign-in Outlook asks for, and name the page each week was read from
  ([#29](https://github.com/CalebSargeant/afas-automation/pull/29),
  [`8a98242`](https://github.com/CalebSargeant/afas-automation/commit/8a9824277876aa5432477265f280db091b119aa4))

### Documentation

- **calendar**: _describe also names a page that read fine
  ([#29](https://github.com/CalebSargeant/afas-automation/pull/29),
  [`8a98242`](https://github.com/CalebSargeant/afas-automation/commit/8a9824277876aa5432477265f280db091b119aa4))


## v1.4.1 (2026-11-06)

### Bug Fixes

- Answer Entra's method picker with the verification code
  ([#28](https://github.com/CalebSargeant/afas-automation/pull/28),
  [`72da603`](https://github.com/CalebSargeant/afas-automation/commit/72da6032d16e0b4cacef64bd3df59f81743f387c))

### Documentation

- Say the submit click runs on the live portal
  ([#27](https://github.com/CalebSargeant/afas-automation/pull/27),
  [`c4344d2`](https://github.com/CalebSargeant/afas-automation/commit/c4344d265edcd1aa419c98f7a57a6c632f78daff))


## v1.4.0 (2026-11-04)

### Features

- File days that come after a month went in as a top-up
  ([#26](https://github.com/CalebSargeant/afas-automation/pull/26),
  [`a476d51`](https://github.com/CalebSargeant/afas-automation/commit/a476d516112f0ebaf602fb0bbc04b97ddc1562a6))


## v1.3.3 (2026-11-04)

### Bug Fixes

- Call days with no ledger row Niet vastgelegd on the card
  ([#25](https://github.com/CalebSargeant/afas-automation/pull/25),
  [`5f172f5`](https://github.com/CalebSargeant/afas-automation/commit/5f172f572ca3949a2000f323ef0ae9185e20cc46))

- Quiet wait behind the app's own draft, keep decided cards, fix handler logging
  ([#25](https://github.com/CalebSargeant/afas-automation/pull/25),
  [`5f172f5`](https://github.com/CalebSargeant/afas-automation/commit/5f172f572ca3949a2000f323ef0ae9185e20cc46))


## v1.3.2 (2026-11-01)

### Bug Fixes

- **calendar**: Reload once when the week header is missing
  ([#24](https://github.com/CalebSargeant/afas-automation/pull/24),
  [`118f3c1`](https://github.com/CalebSargeant/afas-automation/commit/118f3c13a8124131e0948cbbcdb87ee03ad22eb1))


## v1.3.1 (2026-11-01)

### Bug Fixes

- **classify**: Keep a decided day when the calendar read fails
  ([#23](https://github.com/CalebSargeant/afas-automation/pull/23),
  [`2f0c53b`](https://github.com/CalebSargeant/afas-automation/commit/2f0c53bf4efb54c214611f41636d82ef71648ffa))


## v1.3.0 (2026-11-01)

### Bug Fixes

- Close the gaps a review found in the claim path
  ([#21](https://github.com/CalebSargeant/afas-automation/pull/21),
  [`5c8f8b4`](https://github.com/CalebSargeant/afas-automation/commit/5c8f8b4321b8bc9c2bda3b9418d03708d557da08))

- Make build dependable and prove claims against AFAS
  ([#21](https://github.com/CalebSargeant/afas-automation/pull/21),
  [`5c8f8b4`](https://github.com/CalebSargeant/afas-automation/commit/5c8f8b4321b8bc9c2bda3b9418d03708d557da08))

- **ci**: Pin the image workflow back to a version that still has the file
  ([#21](https://github.com/CalebSargeant/afas-automation/pull/21),
  [`5c8f8b4`](https://github.com/CalebSargeant/afas-automation/commit/5c8f8b4321b8bc9c2bda3b9418d03708d557da08))

- **insite**: Find periods the menu does not show, compare counts as numbers
  ([#21](https://github.com/CalebSargeant/afas-automation/pull/21),
  [`5c8f8b4`](https://github.com/CalebSargeant/afas-automation/commit/5c8f8b4321b8bc9c2bda3b9418d03708d557da08))

### Chores

- **deps**: Bump the actions-version group across 1 directory with 2 updates
  ([#3](https://github.com/CalebSargeant/afas-automation/pull/3),
  [`5813a14`](https://github.com/CalebSargeant/afas-automation/commit/5813a14cae996fa228e66767a6ac7fe01b496ff7))

### Documentation

- Describe AFAS read-back, the daily build and alerts
  ([#21](https://github.com/CalebSargeant/afas-automation/pull/21),
  [`5c8f8b4`](https://github.com/CalebSargeant/afas-automation/commit/5c8f8b4321b8bc9c2bda3b9418d03708d557da08))

- List read-back, alerts and submit in the module and workload tables
  ([#21](https://github.com/CalebSargeant/afas-automation/pull/21),
  [`5c8f8b4`](https://github.com/CalebSargeant/afas-automation/commit/5c8f8b4321b8bc9c2bda3b9418d03708d557da08))

### Features

- **build**: Read AFAS before and after every write, and alert on failure
  ([#21](https://github.com/CalebSargeant/afas-automation/pull/21),
  [`5c8f8b4`](https://github.com/CalebSargeant/afas-automation/commit/5c8f8b4321b8bc9c2bda3b9418d03708d557da08))


## v1.2.0 (2026-09-07)

### Bug Fixes

- **m365**: Https-only URLs, unique JSON-RPC ids, and degrade on an unknown block
  ([`52908b6`](https://github.com/CalebSargeant/afas-declaraties/commit/52908b665dc2da4004d1adf21dd7b8e41212c525))

- **m365**: Put the scanner marker on the line it reports
  ([`47ff5d6`](https://github.com/CalebSargeant/afas-declaraties/commit/47ff5d6211a0d3aec86e6390d1a8ccc7815db295))

- **m365_mcp**: Raise M365Error on response missing result key
  ([`8582891`](https://github.com/CalebSargeant/afas-declaraties/commit/85828918402cf55dd9ea8f7b506f7a34a816a28a))

### Features

- **calendar**: Read the calendar over the M365 MCP connector
  ([`7c5b704`](https://github.com/CalebSargeant/afas-declaraties/commit/7c5b7044fea5de285fe1fc7eccf6bf40557b7a7c))

### Testing

- **m365**: Cover the transport paths that decide loud versus silent
  ([`813db8a`](https://github.com/CalebSargeant/afas-declaraties/commit/813db8af1b209bb63ed519c6546b95fd1425d02d))


## v1.1.1 (2026-09-07)

### Bug Fixes

- **override**: Keep the verdict consistent with the claim, and dedupe corrections
  ([`4c1737d`](https://github.com/CalebSargeant/afas-declaraties/commit/4c1737d8aa58bd971586172672a89c9460bec594))


## v1.1.0 (2026-09-06)

### Bug Fixes

- **slackd**: Refresh the heartbeat on a timer, not on Slack traffic
  ([`32399df`](https://github.com/CalebSargeant/afas-declaraties/commit/32399dfddb834783df86978c6232b09da2e8dc2d))

### Features

- **submit**: Run submit on a schedule, and take the period from the approval
  ([`31fbb98`](https://github.com/CalebSargeant/afas-declaraties/commit/31fbb98248b9d1438f8918a50f8dd73eb94456de))


## v1.0.5 (2026-09-05)

### Bug Fixes

- **calendar**: Retry the week header, which re-renders asynchronously
  ([#8](https://github.com/CalebSargeant/afas-declaraties/pull/8),
  [`c23734f`](https://github.com/CalebSargeant/afas-declaraties/commit/c23734f78c75d9af0222fedeec4048032d49b214))


## v1.0.4 (2026-09-05)

### Bug Fixes

- **calendar**: Read the week actually on screen, and navigate to reach others
  ([#7](https://github.com/CalebSargeant/afas-declaraties/pull/7),
  [`4568b11`](https://github.com/CalebSargeant/afas-declaraties/commit/4568b1195b08f14704d3b34d5c47008f3d67dfce))


## v1.0.3 (2026-09-05)

### Bug Fixes

- **ci**: Allowlist TARGETPLATFORM in the Slack-id hygiene rule
  ([#6](https://github.com/CalebSargeant/afas-declaraties/pull/6),
  [`082fe0c`](https://github.com/CalebSargeant/afas-declaraties/commit/082fe0cca3e4ef7b2e4d3f41b3cce83dfaa861ce))

- **ci**: Name the bake target so the release promotes the right image
  ([#6](https://github.com/CalebSargeant/afas-declaraties/pull/6),
  [`082fe0c`](https://github.com/CalebSargeant/afas-declaraties/commit/082fe0cca3e4ef7b2e4d3f41b3cce83dfaa861ce))


## v1.0.2 (2026-09-05)

### Bug Fixes

- **ci**: Document the promote-not-rebuild image flow
  ([#5](https://github.com/CalebSargeant/afas-declaraties/pull/5),
  [`37d20fd`](https://github.com/CalebSargeant/afas-declaraties/commit/37d20fd2c29689176248d1963c5172d61e9321a8))

- **docker**: Skip the Chromium selftest on cross-built platforms
  ([#5](https://github.com/CalebSargeant/afas-declaraties/pull/5),
  [`37d20fd`](https://github.com/CalebSargeant/afas-declaraties/commit/37d20fd2c29689176248d1963c5172d61e9321a8))

### Documentation

- Record why direct pushes to main yield a release with no image
  ([#5](https://github.com/CalebSargeant/afas-declaraties/pull/5),
  [`37d20fd`](https://github.com/CalebSargeant/afas-declaraties/commit/37d20fd2c29689176248d1963c5172d61e9321a8))


## v1.0.1 (2026-09-05)

### Bug Fixes

- **ci**: Drop the concurrency block from the image workflow
  ([`f80b506`](https://github.com/CalebSargeant/afas-declaraties/commit/f80b5065c746804898e8903c6ed060ca6963cdc0))

- **ci**: Pin the image workflow to the reusable version the siblings run
  ([`5ba6864`](https://github.com/CalebSargeant/afas-declaraties/commit/5ba6864fbb8129e28108611e54ef3f8cf1aaf9f7))


## v1.0.0 (2026-09-05)

- Initial Release
