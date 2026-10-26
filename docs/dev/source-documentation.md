# Source documentation maintenance

Use the single project-wide
[source documentation standard](https://github.com/agentic-trpg/agentic-trpg/blob/docs/code-documentation-and-skills/docs/CODE_DOCUMENTATION.md).
For local work, resolve `../agentic-trpg/docs/CODE_DOCUMENTATION.md` from this
repository root in the logical parallel workspace. The linked branch is this coordinated
batch, not a claim of a main merge. New modules/classes/functions need proportionate
docstrings and changed behavior/constraints require synchronized prose independently
of Skill triggering.

The discoverable local workflow is `.agents/skills/trpg-code-documentation/SKILL.md`.
Its SM installation is a byte-identical copy referencing the same authority, with a
matching AST verifier. RE explanations follow [the actual Stateless contract](stateless-evaluation.md),
the [public API](../api.md) and effective FrozenRuleset behavior. They do not claim RE
persists or commits SM world facts; describe Legacy/Bridge runtime paths as such.

Use the existing [validation tooling](validation.md), proportionate to a documentation
change. Syntax, Ruff, strict MkDocs, docstring-stripped AST comparison and relevant
`__doc__`/generated schema consumers are useful; full game suites are not a default for
pure prose. Run the verifier from the repository root with an explicit fixed base:

```powershell
python .agents/skills/trpg-code-documentation/scripts/verify_docstrings.py --base <SHA>
```

Coverage, evidence limits and selected checks are recorded in
[the batch report](source-documentation-acceptance.md).
