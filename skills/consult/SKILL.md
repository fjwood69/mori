- name: consult
- description: Get strategic guidance from the advisor model

1. Parse the user's input:
   - First positional argument is the `question`
   - `--focus`: one of `general`, `architecture`, `security`, `performance`, `style`
   - `--depth`: one of `quick`, `balanced`, `deep`
   - `--file` / `-f`: file path(s) to include as context
2. Call `mori-consult_advisor` with the parsed arguments.
3. Present the result. If it fails, retry once with `--depth quick`.