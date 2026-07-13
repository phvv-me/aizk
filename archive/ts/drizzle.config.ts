import { defineConfig } from 'drizzle-kit';

export default defineConfig({
	dialect: 'postgresql',
	schema: './src/lib/server/db/schema.ts',
	out: './drizzle',
	dbCredentials: { url: process.env.AIZK_ADMIN_DATABASE_URL ?? '' },
	entities: { roles: true },
	migrations: { table: 'drizzle_migrations' }
});
