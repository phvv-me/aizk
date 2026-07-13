// Guards for the SQL template layer: JS numbers erase the int/float distinction, so a bare
// String(number) or template interpolation inside sql.raw once turned Postgres float
// division into integer division. All numeric literals reach SQL through the query
// builder's typed n()/f() helpers, never through ad-hoc stringification.
import tseslint from 'typescript-eslint';

export default tseslint.config(
	...tseslint.configs.recommended,
	{
		rules: {
			'no-restricted-syntax': [
				'error',
				{
					selector:
						"CallExpression[callee.object.name='sql'][callee.property.name='raw'] CallExpression[callee.name='String']",
					message:
						'String(number) erases the int/float distinction in SQL; render through the n()/f() helpers.'
				},
				{
					selector:
						"CallExpression[callee.object.name='sql'][callee.property.name='raw'] TemplateLiteral Identifier[name!='mention']",
					message:
						'Interpolating identifiers into sql.raw bypasses parameter binding and float rendering; use bound params or the n()/f() helpers.'
				}
			]
		}
	},
	{ ignores: ['build/', '.svelte-kit/', 'node_modules/'] }
);
