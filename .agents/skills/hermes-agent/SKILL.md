---
name: hermes-agent-conventions
description: Development conventions and patterns for hermes-agent. Python project with conventional commits.
---

# Hermes Agent Conventions

> Generated from [adm01-debug/hermes-agent](https://github.com/adm01-debug/hermes-agent) on 2026-08-05

## Overview

This skill teaches Claude the development patterns and conventions used in hermes-agent.

## Tech Stack

- **Primary Language**: Python
- **Architecture**: type-based module organization
- **Test Location**: separate
- **Test Framework**: vitest

## When to Use This Skill

Activate this skill when:
- Making changes to this repository
- Adding new features following established patterns
- Writing tests that match project conventions
- Creating commits with proper message format

## Commit Conventions

Follow these commit message conventions based on 42 analyzed commits.

### Commit Style: Conventional Commits

### Prefixes Used

- `fix`
- `feat`
- `chore`

### Message Guidelines

- Average message length: ~63 characters
- Keep first line concise and descriptive
- Use imperative mood ("Add feature" not "Added feature")


*Commit message example*

```text
feat(observability): aggregate bounded tool metrics
```

*Commit message example*

```text
fix(observability): derive tool metrics from runtime metadata
```

*Commit message example*

```text
test(install): prove updating from a release reaches this commit
```

*Commit message example*

```text
ci: test updating from sampled release tags, on tag + every 12h
```

*Commit message example*

```text
chore: suppress windows-footgun false positive on gated killpg
```

*Commit message example*

```text
fix(observability): harden tool lifecycle metrics
```

*Commit message example*

```text
Merge updated model metrics into tool metrics
```

*Commit message example*

```text
Merge current model route metrics into tool metrics
```

## Architecture

### Project Structure: Monorepo

This project uses **type-based** module organization.

### Configuration Files

- `.github/workflows/install-e2e-run.yml`
- `.github/workflows/install-e2e.yml`
- `.github/workflows/docker.yml`
- `.github/workflows/tests.yml`
- `apps/desktop/package.json`
- `apps/desktop/vite.config.ts`
- `.github/workflows/ci.yml`
- `.github/workflows/contributor-check.yml`
- `.github/workflows/deploy-site.yml`
- `.github/workflows/docs-site-checks.yml`
- `.github/workflows/e2e-desktop.yml`
- `.github/workflows/js-autofix.yml`
- `.github/workflows/js-tests.yml`
- `.github/workflows/osv-scanner.yml`
- `Dockerfile`
- `apps/bootstrap-installer/package.json`
- `apps/shared/package.json`
- `.github/workflows/lint.yml`

### Guidelines

- Group code by type (components, services, utils)
- Keep related functionality in the same type folder
- Avoid circular dependencies between type folders

## Code Style

### Language: Python

### Naming Conventions

| Element | Convention |
|---------|------------|
| Files | camelCase |
| Functions | camelCase |
| Classes | PascalCase |
| Constants | SCREAMING_SNAKE_CASE |

### Import Style: Path Aliases (@/, ~/)

### Export Style: Named Exports


*Preferred import style*

```typescript
// Use path aliases for imports
import { Button } from '@/components/Button'
import { useAuth } from '@/hooks/useAuth'
import { api } from '@/lib/api'
```

*Preferred export style*

```typescript
// Use named exports
export function calculateTotal() { ... }
export const TAX_RATE = 0.1
export interface Order { ... }
```

## Testing

### Test Framework: vitest

### File Pattern: `*.test.ts`

### Test Types

- **Unit tests**: Test individual functions and components in isolation
- **Integration tests**: Test interactions between multiple components/services
- **E2e tests**: Test complete user flows through the application

### Mocking: vi.mock


*Test file structure*

```typescript
import { describe, it, expect } from 'vitest'

describe('MyFunction', () => {
  it('should return expected result', () => {
    const result = myFunction(input)
    expect(result).toBe(expected)
  })
})
```

## Error Handling

### Error Handling Style: Try-Catch Blocks


*Standard error handling pattern*

```typescript
try {
  const result = await riskyOperation()
  return result
} catch (error) {
  console.error('Operation failed:', error)
  throw new Error('User-friendly message')
}
```

## Common Workflows

These workflows were detected from analyzing commit patterns.

### Feature Development

Standard feature implementation workflow

**Frequency**: ~9 times per month

**Steps**:
1. Add feature implementation
2. Add tests for feature
3. Update documentation

**Files typically involved**:
- `apps/desktop/electron/*`
- `apps/desktop/src/app/chat/sidebar/*`
- `apps/desktop/src/app/contrib/*`
- `**/*.test.*`
- `**/api/**`

**Example commit sequence**:
```
feat(dev-sandbox): support fake installer / fake main / git clones
feat(observability): aggregate bounded tool metrics
fix(observability): derive tool metrics from runtime metadata
```

### Test Driven Development

Test-first development workflow (TDD)

**Frequency**: ~4 times per month

**Steps**:
1. Write failing test
2. Implement code to pass test
3. Refactor if needed

**Files typically involved**:
- `**/*.test.*`
- `**/*.spec.*`
- `src/**/*`

**Example commit sequence**:
```
test: add tests for user validation
feat: implement user validation
```


## Best Practices

Based on analysis of the codebase, follow these practices:

### Do

- Use conventional commit format (feat:, fix:, etc.)
- Write tests using vitest
- Follow *.test.ts naming pattern
- Use camelCase for file names
- Prefer named exports

### Don't

- Don't use long relative imports (use aliases)
- Don't write vague commit messages
- Don't skip tests for new features
- Don't deviate from established patterns without discussion

---

*This skill was auto-generated by [ECC Tools](https://ecc.tools). Review and customize as needed for your team.*
