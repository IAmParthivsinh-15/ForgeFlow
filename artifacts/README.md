# artifacts/

Evidence ForgeFlow keeps outside the database (spec section 155), such as browser
screenshots taken by the QA agent through Playwright MCP. Mounted at `/artifacts` in the
containers. Metadata (workflow, task, type) is stored in MongoDB; the files are served by
`GET /api/v1/artifacts/{id}`. Contents are git-ignored.
