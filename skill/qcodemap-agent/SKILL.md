---
name: qcodemap-agent
description: Rapidly onboard to the QCodeMap repository, choose the right code-navigation query, and create or review project-specific custom/config.py, custom/facts.py, and custom/seeds.py integrations. Use when an agent must understand QCodeMap, diagnose its index or query behavior, adapt it to a Python project, or add framework facts without leaking project-specific logic into the generic core.
---

# QCodeMap Agent

Use this skill as the shortest reliable route from an unfamiliar checkout to an
evidence-backed QCodeMap result or a correctly layered customization.

## Establish the repository boundary

Resolve the repository root two levels above this file, then keep these layers
separate:

- `qcodemap/`: project-agnostic index, storage, resolver, CLI, and MCP logic.
- `custom/`: the only project-specific boundary. Keep framework names, paths,
  dispatch conventions, and diagnostics here.
- `cache/`: rebuildable SQLite output. Never commit it.
- `tests/`: public tests must use neutral, self-contained fixtures.
- `docs/`: detailed architecture and extension guidance.

Do not move a project convention into `qcodemap/` merely because one project
uses it frequently. Extend the generic fact protocol only when multiple custom
implementations need the same neutral capability.

## Onboard quickly

1. Read the language-appropriate root README for positioning and current CLI
   examples.
2. Read `custom/README.md` before changing a project profile.
3. Read `qcodemap/hooks.py` before implementing facts; treat its current method
   signatures as authoritative.
4. Read `docs/ARCHITECTURE.md` only when changing schema, build phases, resolver
   behavior, freshness, or blast radius.
5. Read `docs/CUSTOM_GUIDE.md` only when a hook needs a fuller example; verify
   its advice against `hooks.py` and current tests because code is authoritative.
6. Run `python -m qcodemap --help` and the relevant subcommand `--help` instead
   of guessing flags.

Inspect the working tree before editing. Preserve local `custom/` files even
when Git ignores them; they may be the active project profile.

## Route the question

| Need | Preferred route |
| --- | --- |
| Exact text, event string, resource name, or unknown symbol | Start with `rg` |
| Definition or all identifier occurrences | `defs` / `usages` (`usages` only accepts one identifier) |
| Verified callers or callees | `callers` / `callees` |
| Unknown file for a known symbol | `callers Class.Func` or `callees Class.Func` (auto-locate or candidates) |
| Ambiguous receiver type | `callers --receiver-class <Class>` |
| String-dispatched RPC or event endpoints | `rpc-refs` / `pubsub-refs` |
| Cross-boundary call route | `path --from Class.Func --to Endpoint.Handle` |
| Imports, reverse imports, hubs, or package shape | `deps` / `importers` / `hubs` / `tree` |
| Working-copy impact | `blast-radius` |
| Project-level custom invariants | `diagnose` |
| Agent-sized file or project context | `find` / `file-context` / `context` |

Prefer JSON for automation and inspect both `coverage` and `index` metadata.
Treat `CANDIDATE` as a lead, not proof. Report `VERIFIED` and
`FRAMEWORK-INFERRED` with their file, line, and evidence note.

## Understand refresh behavior

Do not assume that editing source immediately runs a build. QCodeMap has no
background watcher.

- CLI and MCP queries default to `--refresh auto`: scan the file set and mtimes,
  then incrementally rebuild only added, modified, or deleted files.
- `--refresh check` reports drift without writing the index.
- `--refresh off` accepts the existing index without scanning.
- MCP throttles freshness checks for one second within the process.
- Schema, configuration, index-profile, or custom-hook fingerprint changes
  reject the old index. Run an explicit full `build --rebuild`; do not hide a
  multi-minute rebuild inside a query.
- A scoped `build --targets` refreshes and prunes only that scope. Use
  `--rebuild --targets` only when intentionally creating a subset database.

## Create a project profile

Create only the files the project needs:

### `custom/config.py`

Use neutral path rules and keep generated-data policy explicit:

```python
ROOT = r'D:\code\your-project'
TARGETS = ['client', 'server', 'shared']
EXCLUDE_DIRS = {'__pycache__', '.git', '.svn', 'venv'}
EXCLUDE_FILES = ['*_generated_legacy.py']
INCLUDE_PATHS = []

INDEX_PROFILE_RULES = [
    ('client/data/**', 'semantic-only'),
    ('server/data/**', 'semantic-only'),
]

RPC_CHANNELS = {
    'C2S': 'client to server',
    'S2C': 'server to client',
}
```

Use `full` for normal code. Use `semantic-only` for generated or data-heavy
paths where definitions, imports, and semantic facts matter but identifier
occurrences would dominate the database.

### `custom/seeds.py`

Add seeds only for facts AST and hooks cannot derive deterministically:

```python
RET_SEEDS = {
    ('Factory', 'CreateTarget'): 'FestivalTargetEntity',
}

ATTR_SEEDS = {
    ('Controller', 'active_target'): 'FestivalTargetEntity',
}
```

Comment approximate seeds. Prefer a hook when a stable syntax pattern can
derive the fact for every occurrence.

### `custom/facts.py`

Subclass `FactsHooks` and override only the required methods:

| Hook | Fact produced |
| --- | --- |
| `assign_value_type` | Type evidence for assignments |
| `class_facts` | Component/injection facts from class decorators |
| `method_alias_facts` | Physical source method to runtime method-name rewrites |
| `class_stmt_fact` | Attribute or global-assignment facts from class statements |
| `importall_members` | Project naming convention for imported components |
| `rpc_facts` | String-dispatched RPC calls |
| `pubsub_facts` | Event publishers and subscribers |
| `callback_facts` | Declarative callback relationships |
| `call_callback_facts` | Registered bound-method callbacks in function bodies |
| `receiver_type_facts` | Receiver type evidence at a call site |
| `expand_receiver_type` | Query-time expansion of project pseudo-types |
| `file_partition` | Client/server or other resolver partitions |
| `handler_facts` | RPC handler direction, endpoint, and confidence |
| `endpoint_aliases` | Equivalent cross-end or runtime endpoint names |
| `project_diagnostics` | Project-specific consistency issues |

Return `[]` or `None` when evidence is insufficient. Never manufacture a type
from a weak naming coincidence alone.

## Use the sanitized custom pattern

The following pattern is distilled from a Messiah integration but intentionally
uses generic names. It demonstrates the fact boundary only; it does not describe
an engine, its modules, or a project architecture.

```python
import ast

from qcodemap.hooks import FactsHooks
from qcodemap.scanner import dotted


class ProjectFacts(FactsHooks):

    def callback_facts(self, stmt, ctx):
        # SyncedField('state', ...) -> _on_set_state(old)
        if not (isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and dotted(stmt.value.func) == 'SyncedField'):
            return []
        args = stmt.value.args
        if not (args and isinstance(args[0], ast.Constant)
                and isinstance(args[0].value, str)):
            return []
        name = args[0].value
        return [('SYNCED_FIELD', name, '_on_set_%s' % name)]

    def rpc_facts(self, call, ctx):
        # transport.SendRequest('ActivateTarget', ...)
        if not (isinstance(call.func, ast.Attribute)
                and call.func.attr == 'SendRequest'
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)):
            return []
        return [('C2S', call.args[0].value, None)]

    def receiver_type_facts(self, call, ctx):
        # A type guard supplies stronger evidence than a same-name method.
        if not isinstance(call.func, ast.Attribute):
            return []
        receiver = dotted(call.func.value)
        if not receiver or ctx.function_node is None:
            return []
        local_name = receiver.split('.', 1)[0]
        guarded = any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == local_name
            and node.attr == 'IsFestivalTarget'
            for node in ast.walk(ctx.function_node))
        if not guarded:
            return []
        return [(receiver, 'FestivalTargetEntity', 'framework',
                 'explicit target type guard')]

    def handler_facts(self, fn, ctx):
        for decorator in fn.decorator_list:
            if (isinstance(decorator, ast.Call)
                    and dotted(decorator.func) == 'endpoint_handler'
                    and decorator.args
                    and dotted(decorator.args[0]) == 'INBOUND'):
                return [('C2S', fn.name, ctx.cls, 'verified',
                         '@endpoint_handler(INBOUND)')]
        return []
```

Before adapting this example, inspect real definitions and call signatures with
`rg`. Confirm argument positions, decorator flags, and direction from source;
do not infer them from one call sample.

Keep project diagnostics in `project_diagnostics`. Query indexed facts where
possible, return structured issue dictionaries, and avoid teaching the core a
project's field names or symmetry rules.

## Validate an adaptation

1. Build a temporary or dedicated database with `build --rebuild`.
2. Query one known positive and one known collision for every new fact type.
3. Confirm receiver evidence removes irrelevant same-name candidates without
   converting weak evidence into `VERIFIED`.
4. Confirm RPC output distinguishes verified/inferred handlers from `NAME-ONLY`.
5. Run `diagnose` before and after a fixture fix when adding a diagnostic.
6. Modify, add, and delete a temporary file; confirm `auto`, `check`, and `off`
   behave differently as documented.
7. Run `python tests/test_p4.py` through `python tests/test_p8.py` for core changes.
8. Run project-local regressions when local custom files are available.
9. Increment `RESOLVER_VERSION` for resolver behavior changes and
   `SCHEMA_VERSION` for storage changes.

Separate static proof from runtime proof. QCodeMap indexes source facts; it does
not establish actual runtime behavior.

## Protect public boundaries

- Keep real project roots, internal module paths, class names, dispatcher names,
  entity registries, and architecture descriptions out of public examples.
- Replace every project identifier with a neutral fixture before publishing.
- Do not copy a local ignored `custom/` profile into a public commit unless the
  user explicitly confirms it is sanitized and publishable.
- Keep public core tests self-contained; never require a private checkout.
- Preserve user-owned working-tree changes and report ignored custom files
  separately because ordinary `git status` does not show them.

When reporting results, include the query, confidence level, source location,
coverage state, freshness state, and any validation that remains unperformed.
