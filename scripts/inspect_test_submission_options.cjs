const fs = require("fs");
const { Client } = require("C:/tmp/codex-pg-client/node_modules/pg");

const match = fs.readFileSync(".env", "utf8").match(/^DATABASE_URL=(.+)$/m);
if (!match) throw new Error("DATABASE_URL is not configured");

const client = new Client({ connectionString: match[1].trim(), ssl: { rejectUnauthorized: false } });

(async () => {
  await client.connect();
  try {
    const [creator, brands, accounts, reviewers] = await Promise.all([
      client.query("SELECT id FROM users WHERE lower(email) = lower($1) AND deleted_at IS NULL", ["test.creator@example.com"]),
      client.query("SELECT brand_code FROM user_brands WHERE user_id = (SELECT id FROM users WHERE lower(email) = lower($1))", ["test.creator@example.com"]),
      client.query("SELECT id, provider, account_name, connection_metadata FROM oauth_connections ORDER BY created_at LIMIT 10"),
      client.query("SELECT id, email, full_name FROM users WHERE is_super_admin AND is_active AND deleted_at IS NULL ORDER BY created_at LIMIT 5"),
    ]);
    console.log(JSON.stringify({ creator: creator.rows, brands: brands.rows, accounts: accounts.rows, reviewers: reviewers.rows }));
  } finally {
    await client.end();
  }
})();
