# CHANGELOG

<!-- version list -->

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
