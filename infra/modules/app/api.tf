# The HTTP API (design.md sections 5 and 11): one route, POST /ask, with AWS_IAM
# authorization, integrated with rm-router through payload format 2.0.

resource "aws_apigatewayv2_api" "ask" {
  name          = local.api_name
  description   = "Rollout Marshal's assistant API: ${local.names.API_ROUTE_KEY} to ${local.names.ROUTER_FUNCTION}."
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "router" {
  api_id                 = aws_apigatewayv2_api.ask.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn["router"].invoke_arn
  payload_format_version = local.names.API_PAYLOAD_FORMAT_VERSION
}

resource "aws_apigatewayv2_route" "ask" {
  api_id             = aws_apigatewayv2_api.ask.id
  route_key          = local.names.API_ROUTE_KEY
  authorization_type = local.names.API_AUTHORIZATION_TYPE
  target             = "integrations/${aws_apigatewayv2_integration.router.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.ask.id
  name        = "$default"
  auto_deploy = true
}

# API Gateway may invoke rm-router for this route only.
resource "aws_lambda_permission" "api" {
  statement_id  = "AllowTheAskRoute"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["router"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ask.execution_arn}/*/${split(" ", local.names.API_ROUTE_KEY)[0]}${local.route_path}"
}
