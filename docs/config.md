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
| `model` | no | `global.anthropic.claude-sonnet-4-6` | Bedrock model ID or inference profile ID/ARN used to classify |
| `output` | no | `report` | Report path without extension. A local path or `s3://bucket/key` |
| `format` | no | `all` | `html`, `md`, `json`, `both` (html and md) or `all` |
| `max_use_cases` | no | `50` | Use cases assessed one by one. The rest roll into "Other" |
| `show_samples` | no | `false` | `true` adds raw prompt and response excerpts to the report |

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

The identity running aiva needs:

1. `s3:ListBucket` and `s3:GetObject` on each source bucket, plus `kms:Decrypt` if
   the bucket uses a customer managed KMS key.
2. `bedrock:InvokeModel` on the classification model (Converse uses this action).
   A `global.` or geo profile can route to other regions, so allow the foundation
   model in every region: `arn:aws:bedrock:*::foundation-model/*`.
3. `sts:AssumeRole` on any `role_arn`, and that role's trust policy must allow it.
4. `s3:PutObject` on the output bucket if `output` is an `s3://` path.

[`iam-policy.json`](iam-policy.json) covers 1 and 2.

## Network and endpoints

Proxies, VPC endpoints, FIPS endpoints and custom CA bundles are not config
settings. The AWS SDK reads them from the standard places: `HTTPS_PROXY`,
`AWS_USE_FIPS_ENDPOINT`, `AWS_CA_BUNDLE` and `~/.aws/config`.
