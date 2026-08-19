# AI Value Assessment

AI Value Assessment reads your Bedrock Model Invocation Logs and produces a
report that, for each business use case in those logs, recommends whether to
**STOP**, **REFINE**, or **EXPAND** it, and labels it as a one-off experiment or
a recurring workflow.

It classifies the business task behind each call, not the tool that made it.
Model Invocation Logging captures every Bedrock call regardless of source
(Claude Code, Codex, Amazon Q Developer, LibreChat, or a plain SDK script), so
the tool works unmodified across all of them.

There are two ways to run it: a CloudFormation stack that runs the audit as a
CodeBuild job inside your account (recommended), or a local CLI.

## Deploy with CloudFormation (recommended)

The audit runs as a CodeBuild job inside your account and writes the report to
an S3 bucket. Nothing leaves AWS.

In the CloudFormation console, create a stack and upload
[`cloudformation/deploy.yaml`](cloudformation/deploy.yaml) as the template.

1. Switch to the same region as your Model Invocation Logs bucket. Model access,
   IAM, and logging config are all regional, and a cross-region deploy fails
   pre-flight checks.
2. Set **SourceBucketName** to your log bucket. Optionally set
   **DestinationBucketName**, or leave it blank to have one generated.
3. Optionally set **ReportReaderPrincipalArn** to the single IAM identity that
   may read the report. The report contains real prompt and response excerpts.
   Left blank, any principal in the account can read it. Public access is always
   blocked.
4. Acknowledge the IAM-resources checkbox and create the stack. The audit starts
   when the stack finishes deploying.
5. The report lands in the destination bucket (`DestinationBucketNameOutput`)
   when the CodeBuild project (`AuditProjectConsoleUrl`) finishes.

The stack creates a CodeBuild project, an IAM role scoped to
[`docs/iam-policy.json`](docs/iam-policy.json) plus write to the output bucket,
and the destination S3 bucket (encrypted, public-access blocked, HTTPS-only).
To deploy you need only `codebuild:StartBuild`, `codebuild:BatchGetBuilds`, and
read on the output bucket. The CodeBuild role holds the log-bucket read and
Bedrock invoke, not your own identity.

## Local CLI

The `aiva` package also runs on your machine. The audit runs as a local process
using your AWS credentials, and both the working data and the report land on
local disk with real prompt and response content included. Prefer the
CloudFormation deploy above unless you have a reason not to.

```bash
git clone https://github.com/aws-samples/sample-ai-value-assessment.git
cd sample-ai-value-assessment
uv venv && source .venv/bin/activate
uv pip install -e .

export AWS_PROFILE=your-profile

aiva audit \
  --bucket <your-model-invocation-logs-bucket> \
  --prefix bedrock-logs \
  --region <your-region> \
  --days 7 \
  --output report
```

This writes `report.html`, `report.md`, and `report.json`. These contain real
prompt and response excerpts. Treat them as sensitive: do not commit or share
them externally. Pass `--db path/to/audit.db` to keep the working store and
resume an interrupted run.

## Reading the report

Each use case carries one recommendation:

- **STOP**: no identifiable task, or work that does not need AI.
- **REFINE**: real value but inefficient (wrong model tier, no caching, bloated prompts).
- **EXPAND**: clear value, efficient, worth scaling.

A separate axis labels each use case **experimental** or **repeatable**. A
repeatable use case running outside sanctioned channels is shadow AI worth
surfacing to your platform or security team.

Cost-optimisation checks (model right-sizing, caching, tagging, output
guardrails) are shown per use case. Cost and monthly projection are computed in
code, not by the model.

Classification is semantic. Treat each recommendation as a starting point for
human review, not automated action.

**Privacy note:** Example tasks shown per use case are model-generated
paraphrases, not verbatim quotes from logs. The model is instructed to
de-identify (remove names, project names, customer names), but this is
best-effort. Review the report before sharing it outside your immediate team.

## How it works

![Architecture diagram](docs/architecture.png)

The audit runs in three passes over the logs:

```
S3 logs -> read + normalise -> group into sessions -> Pass 1 -> Pass 2a -> Pass 2b -> report
```

1. **Describe** each session's underlying business task, looking through the
   tool to the real work.
2. **Cluster** activities into distinct use cases by meaning.
3. **Assess** each use case: STOP/REFINE/EXPAND, experimental/repeatable, and
   cost checks.

## Costs

The audit calls Bedrock a handful of times (roughly one classification call per
session plus a couple of rollup calls) using a small model. The expensive
Bedrock usage is your existing logs; the tool only reads and summarises them.

The table below is illustrative, not a quote. Model Invocation Logging writes
one object per Bedrock call, and cost scales with the number of distinct
sessions, not raw invocation count. Rates are the default classification model,
Sonnet 4.6, at $3.00 per million input tokens and $15.00 per million output.

| Log volume (1 week) | Objects / invocations | Distinct sessions | Approx cost to run |
|---------------------|-----------------------|-------------------|--------------------|
| Small team          | ~1,000                | ~20               | a few cents        |
| Department          | ~20,000               | ~400              | ~$1-3              |
| Org-wide            | ~200,000              | ~4,000            | ~$20-40            |

The middle row is close to a real one-week run measured during development.
Actual cost depends on prompt sizes and how many sessions your logs contain,
so treat these as order-of-magnitude.

## Cleanup

CloudFormation deploy: delete the stack in the CloudFormation console (or
`aws cloudformation delete-stack --stack-name <name>`). This removes the
CodeBuild project and IAM role. If you let CloudFormation generate the
destination bucket, empty it first, then it is removed with the stack; a bucket
you named yourself is left in place. There are no other standing resources.

Local CLI: delete the generated `report.*` files and any `--db` store, and
remove the virtualenv.

## Disclaimer

This is sample code, provided as-is, for demonstration purposes only. It is not
an AWS-supported production tool. Review the IAM policy in
`docs/iam-policy.json` and the code itself before running it against any
account, and test in a non-production environment first.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

Licensed under the MIT-0 License. See the `LICENSE` file.
