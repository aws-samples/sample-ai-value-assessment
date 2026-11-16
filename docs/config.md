# Config file

`aiva run --config aiva.toml` reads a [TOML](https://toml.io) file. Start from
[`aiva.example.toml`](../aiva.example.toml). Unknown or missing settings stop the
run with a message naming them.

## Run settings

| Setting | Required | Default | What it does |
|---|---|---|---|
| `region` | yes | | Region aiva calls Bedrock in to classify. Also the default region for sources |
| `from` | yes | | Start date, e.g. `2026-10-01` (UTC) |
| `to` | no | now | End date, inclusive. A date includes that whole day |
| `model` | no | `global.anthropic.claude-sonnet-5-5` | Bedrock model ID or inference profile ID/ARN used to classify |
| `output` | no | `report` | Report path without extension. A local path or `s3://bucket/key` |
| `format` | no | `all` | `html`, `md`, `json`, `both` (html and md) or `all` |
| `max_use_cases` | no | `50` | Use cases assessed one by one. The rest roll into "Other" |
| `show_samples` | no | `false` | `true` adds raw prompt and response excerpts to the report |
| `input_per_1m` | no | `3.0` | USD per 1M input tokens for the cost estimate, the same for every model. Cache reads cost 0.1x and cache writes 1.25x this rate |
| `output_per_1m` | no | `15.0` | USD per 1M output tokens for the cost estimate |

The window is based on when log objects were written, so calls in the last few
minutes before `from` or `to` can fall on either side.

## Sources

One `[[sources]]` block per log location. All sources go into one report.

| Setting | Required | Default | What it does |
|---|---|---|---|
| `type` | yes | | `bedrock` (Model Invocation Logs) or `otlp` (OpenTelemetry JSON) |
| `bucket` | yes | | S3 bucket the logs are in |
| `prefix` | no | `""` | Key prefix. For Bedrock, the prefix set in the logging config, often empty |
| `region` | no | run `region` | Region of the bucket |
| `profile` | no | | AWS config profile to read this source with |
| `role_arn` | no | | Role to assume to read this source, e.g. in a central log archive account |

Only add an `otlp` source for usage that does not run through Bedrock. Claude Code
or Cowork on Bedrock already appear in the Bedrock logs, and would be counted twice.

## Permissions

See [Before you start](../README.md#before-you-start) in the README.

## Network and endpoints

Proxies, VPC endpoints, FIPS endpoints and custom CA bundles are not config
settings. The AWS SDK reads them from the standard places: `HTTPS_PROXY`,
`AWS_USE_FIPS_ENDPOINT`, `AWS_CA_BUNDLE` and `~/.aws/config`.
