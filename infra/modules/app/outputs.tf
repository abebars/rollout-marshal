# ask_url for scripts/load.py (design.md section 11), and rm-answer-cheaper's names for the
# FIS module (task 4.3), which attaches to the function without editing B's files.

output "ask_url" {
  description = "The full URL of the API's only route, POST /ask."
  value       = "${aws_apigatewayv2_api.ask.api_endpoint}${local.route_path}"
}

output "cheaper_function_name" {
  value = aws_lambda_function.fn["cheaper"].function_name
}

output "cheaper_role_name" {
  value = aws_iam_role.fn["cheaper"].name
}
