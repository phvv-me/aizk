#!/bin/sh
set -eu

aws_cli_image="public.ecr.aws/aws-cli/aws-cli:2.36.20"
aws_config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
aws_config_dir="${AIZK_AWS_CONFIG_DIR:-${aws_config_root}/aizk/aws}"
aws_input_dir="${AIZK_AWS_INPUT_DIR:-}"

umask 077
mkdir -p "$aws_config_dir/login"

if [ -n "$aws_input_dir" ]; then
    exec docker run --rm --interactive \
        --user "$(id -u):$(id -g)" \
        --env HOME=/aws-home \
        --env AWS_ACCESS_KEY_ID \
        --env AWS_SECRET_ACCESS_KEY \
        --env AWS_SESSION_TOKEN \
        --env AWS_PROFILE \
        --env AWS_DEFAULT_PROFILE \
        --env AWS_REGION \
        --env AWS_DEFAULT_REGION \
        --env AWS_CLI_AUTO_PROMPT \
        --env AWS_PAGER= \
        --volume "$aws_config_dir:/aws-home/.aws" \
        --volume "$PWD:/aws" \
        --volume "$aws_input_dir:/aws-input:ro" \
        "$aws_cli_image" \
        "$@"
fi

exec docker run --rm --interactive \
    --user "$(id -u):$(id -g)" \
    --env HOME=/aws-home \
    --env AWS_ACCESS_KEY_ID \
    --env AWS_SECRET_ACCESS_KEY \
    --env AWS_SESSION_TOKEN \
    --env AWS_PROFILE \
    --env AWS_DEFAULT_PROFILE \
    --env AWS_REGION \
    --env AWS_DEFAULT_REGION \
    --env AWS_CLI_AUTO_PROMPT \
    --env AWS_PAGER= \
    --volume "$aws_config_dir:/aws-home/.aws" \
    --volume "$PWD:/aws" \
    "$aws_cli_image" \
    "$@"
