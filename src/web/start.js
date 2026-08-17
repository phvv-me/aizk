import { GetParametersCommand, SSMClient } from '@aws-sdk/client-ssm';

const parameterNames = JSON.parse(process.env.AIZK_AWS_PARAMETER_ENV ?? '{}');
const entries = Object.entries(parameterNames);

if (entries.length > 0) {
  const response = await new SSMClient({}).send(
    new GetParametersCommand({
      Names: entries.map(([, name]) => name),
      WithDecryption: true
    })
  );
  const parameters = new Map(response.Parameters?.map(({ Name, Value }) => [Name, Value]));
  for (const [variable, name] of entries) {
    const value = parameters.get(name);
    if (!value) throw new Error(`missing required AWS parameter ${name}`);
    process.env[variable] = value;
  }
}

await import('./build/index.js');
