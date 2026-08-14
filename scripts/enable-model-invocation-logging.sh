#!/bin/bash
set -euo pipefail

# Derives the account from your current credentials; override region/bucket/prefix
# via env vars if you want. Nothing is hardcoded to a specific account.
REGION="${REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET_NAME="${BUCKET_NAME:-aiva-model-invocation-logs-${ACCOUNT_ID}-${REGION}}"
PREFIX="${PREFIX:-bedrock-logs}"

echo "=== AI Value Assessment: Enable Model Invocation Logging ==="
echo "Account: ${ACCOUNT_ID}"
echo "Region:  ${REGION}"
echo "Bucket:  ${BUCKET_NAME}"
echo ""

# Step 1: Create S3 bucket
echo "[1/4] Creating S3 bucket..."
if aws s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
    echo "  Bucket already exists, skipping creation."
else
    aws s3api create-bucket \
        --bucket "${BUCKET_NAME}" \
        --region "${REGION}" \
        --create-bucket-configuration LocationConstraint="${REGION}"
    echo "  Bucket created."
fi

# Step 2: Disable ACLs (enforce bucket owner)
echo "[2/4] Enforcing bucket ownership (disabling ACLs)..."
aws s3api put-bucket-ownership-controls \
    --bucket "${BUCKET_NAME}" \
    --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
echo "  Done."

# Step 3: Attach bucket policy for Bedrock
echo "[3/4] Attaching bucket policy..."
POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AmazonBedrockLogsWrite",
            "Effect": "Allow",
            "Principal": {
                "Service": "bedrock.amazonaws.com"
            },
            "Action": [
                "s3:PutObject"
            ],
            "Resource": [
                "arn:aws:s3:::${BUCKET_NAME}/${PREFIX}/AWSLogs/${ACCOUNT_ID}/BedrockModelInvocationLogs/*"
            ],
            "Condition": {
                "StringEquals": {
                    "aws:SourceAccount": "${ACCOUNT_ID}"
                },
                "ArnLike": {
                    "aws:SourceArn": "arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:*"
                }
            }
        },
        {
            "Sid": "DenyPublicAccess",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [
                "arn:aws:s3:::${BUCKET_NAME}",
                "arn:aws:s3:::${BUCKET_NAME}/*"
            ],
            "Condition": {
                "Bool": {
                    "aws:SecureTransport": "false"
                }
            }
        }
    ]
}
EOF
)

aws s3api put-bucket-policy \
    --bucket "${BUCKET_NAME}" \
    --policy "${POLICY}"
echo "  Done."

# Step 4: Enable model invocation logging
echo "[4/4] Enabling model invocation logging in Bedrock..."
LOGGING_CONFIG=$(cat <<EOF
{
    "loggingConfig": {
        "textDataDeliveryEnabled": true,
        "imageDataDeliveryEnabled": true,
        "embeddingDataDeliveryEnabled": true,
        "s3Config": {
            "bucketName": "${BUCKET_NAME}",
            "keyPrefix": "${PREFIX}"
        }
    }
}
EOF
)

aws bedrock put-model-invocation-logging-configuration \
    --region "${REGION}" \
    --cli-input-json "${LOGGING_CONFIG}"
echo "  Done."

echo ""
echo "=== Model Invocation Logging Enabled ==="
echo "Logs will appear at: s3://${BUCKET_NAME}/${PREFIX}/AWSLogs/${ACCOUNT_ID}/BedrockModelInvocationLogs/"
echo ""
echo "Verify with:"
echo "  aws bedrock get-model-invocation-logging-configuration --region ${REGION}"
