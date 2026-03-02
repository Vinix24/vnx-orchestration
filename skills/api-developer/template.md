# API Developer Report Template

## Output Location
Write your report to: `.vnx-data/unified_reports/`

## Filename Format
`{timestamp}-{track}-api-{title}.md`

Example: `20260202-143000-A-api-user-endpoints.md`

## Report Structure

### Summary
Brief overview of API work completed (2-3 sentences)

### Tags (Required)
- [tag1, tag2, tag3]  # Use specific, compound tags (e.g., sse-streaming, browser-pool, kvk-validation). Avoid general-only tags.

### API Implementation
- Endpoints created/modified
- HTTP methods and routes
- Request/response schemas
- Authentication/authorization

### Code Changes
- Controller functions
- Middleware added
- Route configurations
- Schema definitions

### API Documentation
- OpenAPI/Swagger updates
- Endpoint descriptions
- Example requests/responses
- Error codes documented

### Testing
- API tests written
- Postman/Insomnia collections
- Load testing results (if applicable)
- Integration test coverage

### Evidence
- Working API calls with curl examples
- Response examples
- Performance metrics
- Test output

### Recommendations
- Security improvements
- Performance optimizations
- API versioning considerations
- Next endpoints to implement

### Intelligence Feedback
<!-- If your dispatch included [INTELLIGENCE_DATA] with offered_pattern_hashes, report which patterns you actually used: -->
used_pattern_hashes: []

## Quality Standards
- Follow RESTful conventions
- Include complete OpenAPI documentation
- Provide curl examples for all endpoints
- Document all error responses
