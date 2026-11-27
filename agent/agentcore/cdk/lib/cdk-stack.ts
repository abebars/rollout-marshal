import { AgentCoreApplication, type AgentCoreApplicationProps } from '@aws/agentcore-cdk';
import { Stack, type StackProps, aws_iam as iam } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { existsSync, readFileSync } from 'fs';
import { dirname, join } from 'path';

export interface AgentCoreStackProps extends StackProps {
  /** Props for the AgentCore application, produced by transformAgentCoreJson from agentcore/agentcore.json. */
  application: AgentCoreApplicationProps;
}

/**
 * The repo's contracts/names.json, generated from contracts/names.py (make names-json),
 * found by walking up from this file. Every name below comes from it.
 */
function contractNames(): Record<string, unknown> {
  let directory = __dirname;
  for (;;) {
    const candidate = join(directory, 'contracts', 'names.json');
    if (existsSync(candidate)) {
      return JSON.parse(readFileSync(candidate, 'utf8')) as Record<string, unknown>;
    }
    const parent = dirname(directory);
    if (parent === directory) {
      throw new Error(`contracts/names.json not found above ${__dirname}`);
    }
    directory = parent;
  }
}

function name(names: Record<string, unknown>, key: string): string {
  const value = names[key];
  if (typeof value !== 'string' || value === '') {
    throw new Error(`contracts/names.json has no string ${key}`);
  }
  return value;
}

/**
 * The stack the AgentCore CLI deploys. Everything declared in agentcore/agentcore.json is
 * created by the AgentCoreApplication construct. Rollout Marshal adds only what its runtime
 * needs on top (design.md section 7): calling rm-gateway, which agent/scripts/setup_gateway.py
 * creates outside this stack, and finding that Gateway's URL by its name.
 */
export class AgentCoreStack extends Stack {
  public readonly application: AgentCoreApplication;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);
    this.application = new AgentCoreApplication(this, 'Application', props.application);

    const names = contractNames();
    const runtime = this.application.runtime(name(names, 'AGENT_RUNTIME_NAME'));
    // A Gateway's ID is its name plus a ten-character suffix, so this matches rm-gateway only.
    const gatewayArn = this.formatArn({
      service: 'bedrock-agentcore',
      resource: 'gateway',
      resourceName: `${name(names, 'GATEWAY_NAME')}-*`,
    });
    runtime.grantPrincipal.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'UseRmGateway',
        actions: [name(names, 'GATEWAY_INVOKE_ACTION'), 'bedrock-agentcore:GetGateway'],
        resources: [gatewayArn],
      }),
    );
    // ListGateways has no resource type (Service Authorization Reference), so it takes "*".
    runtime.grantPrincipal.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'FindRmGateway',
        actions: ['bedrock-agentcore:ListGateways'],
        resources: ['*'],
      }),
    );
  }
}
