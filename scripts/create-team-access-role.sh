#!/bin/bash
set -euo pipefail

# Optional helper: creates an IAM role others can assume to run AI Value Assessment against
# this account. Not required for the basic local-CLI workflow. Account is
# derived from your current credentials; override region/names via env vars.
REGION="${REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ROLE_NAME="${ROLE_NAME:-aiva-bedrock-user}"
POLICY_NAME="${POLICY_NAME:-aiva-bedrock-invoke}"
TEAM_TAG="${TEAM_TAG:-aiva}"

echo "=== AI Value Assessment: Create Team Access Role ==="
echo "Account: ${ACCOUNT_ID}"
echo "Region:  ${REGION}"
echo "Role:    ${ROLE_NAME}"
echo ""

# Step 1: Create the IAM role with trust policy
echo "[1/3] Creating IAM role..."

# Trust policy: allow specified accounts to assume this role
# For same-account users, trust the account itself
TRUST_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::${ACCOUNT_ID}:root"
            },
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringLike": {
                    "aws:PrincipalTag/team": "${TEAM_TAG}"
                }
            }
        }
    ]
}
EOF
)

if aws iam get-role --role-name "${ROLE_NAME}" 2>/dev/null; then
    echo "  Role already exists, updating trust policy..."
    aws iam update-assume-role-policy \
        --role-name "${ROLE_NAME}" \
        --policy-document "${TRUST_POLICY}"
else
    aws iam create-role \
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}" \
        --description "AI Value Assessment - Bedrock access for team evaluation" \
        --tags Key=project,Value=aiva "Key=team,Value=${TEAM_TAG}"
    echo "  Role created."
fi

# Step 2: Attach Bedrock invoke permissions (scoped to inference only, no admin)
echo "[2/3] Attaching Bedrock permissions..."

INVOKE_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "BedrockInvoke",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:Converse",
                "bedrock:ConverseStream"
            ],
            "Resource": [
                "arn:aws:bedrock:${REGION}::foundation-model/*",
                "arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:inference-profile/*",
                "arn:aws:bedrock:us:${ACCOUNT_ID}:inference-profile/*"
            ]
        },
        {
            "Sid": "BedrockListModels",
            "Effect": "Allow",
            "Action": [
                "bedrock:ListFoundationModels",
                "bedrock:ListInferenceProfiles",
                "bedrock:GetFoundationModel"
            ],
            "Resource": "*"
        }
    ]
}
EOF
)

aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "${POLICY_NAME}" \
    --policy-document "${INVOKE_POLICY}"
echo "  Done."

# Step 3: Output instructions
echo "[3/3] Setup complete."
echo ""
echo "=== Team Member Instructions ==="
echo ""
echo "To use this role, team members need to assume it:"
echo ""
echo "  aws sts assume-role \\"
echo "    --role-arn arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME} \\"
echo "    --role-session-name \$(whoami)-aiva"
echo ""
echo "Or configure in ~/.aws/config:"
echo ""
echo "  [profile aiva]"
echo "  role_arn = arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo "  source_profile = default"
echo "  region = ${REGION}"
echo ""
echo "Then use: AWS_PROFILE=aiva aws bedrock-runtime converse ..."
echo ""
echo "Model Invocation Logging is already enabled."
echo "All calls through this role will be captured in:"
echo "  s3://aiva-model-invocation-logs-${ACCOUNT_ID}-${REGION}/bedrock-logs/"
