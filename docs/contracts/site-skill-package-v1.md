# Site Skill filesystem package and CLI contract

Packages live at `web_listening/skills/sites/<site_key>/<version>/`. Each package
must contain non-empty `manifest.json`, `SKILL.md`, `profiles/`, `scripts/`, and
`tests/` content. The registry is read-only and static: it performs no imports,
execution, network access, or DNS resolution. Every symlink and non-regular file
is rejected.

Validation is bounded to 1,024 regular files, a proportional package traversal
budget, 4,194,304 bytes per file, and 33,554,432 aggregate file bytes per
package. Crossing either the file or traversal ceiling stops traversal with one
stable `package.file_count_limit` diagnostic. Byte limits are rejected with
stable structured diagnostics before excess contents are loaded.
Package validation returns at most 1,024 diagnostics across all validation
categories. When additional findings exist, the bounded result includes one
stable `package.diagnostic_limit` truncation diagnostic and never includes
credential or profile values.
Registry discovery also caps both the registry root and each site directory at
1,024 entries before sorting. Exceeding either ceiling, or exhausting memory
during enumeration, fails closed with a stable structured registry/site
diagnostic.

The package digest uses SHA-256 with the initial bytes
`web-listening.site-skill-package.v1` followed by NUL. For each regular file in
ascending POSIX-relative UTF-8 path order, hash an unsigned 8-byte big-endian
path-byte length, the path bytes, an unsigned 8-byte big-endian content length,
and the exact content bytes. `manifest_sha256` and each referenced script digest
hash exact raw bytes directly. `package_sha256` is `null` whenever bounded
validation cannot read every governed regular file because of a size, count,
aggregate, read, or resource violation; a retained subset is never labeled with
an exact package digest. Exact selection only resolves a valid package with a
complete digest.

`list-site-skills`, `inspect-site-skill`, and `validate-site-skill` emit stable
JSON envelopes `site-skill-list.v1`, `site-skill-inspect.v1`, and
`site-skill-validation.v1`. Exact selection requires `site_key`, canonical
version, and lowercase package SHA-256, and fails unless exactly one candidate
matches. Validation accepts either `--package-path` or the complete exact
selector set, never both. The canonical digest option is `--package-digest`;
`--package-sha256` remains an alias. Profiles must declare a non-empty subset of
manifest `allowed_domains`; every nested URL-like value must be absolute HTTP(S),
contain no userinfo, and target one of those manifest domains or a subdomain.
`tests/verification.json` declares `implemented_rule_ids`.
When `--json` is present, parser failures for these three commands use the same
versioned envelope on stdout, leave stderr empty, and exit nonzero.

An explicitly supplied registry root must be an existing, readable, real
directory; missing, symlinked, and unreadable explicit roots return a structured
`registry.invalid_root` result and a nonzero CLI status. Only the packaged
default root may be absent and mean an empty valid registry, which supports
minimal installations that intentionally omit package data.

Governed text rejects literal credentials and applies `manifest.secret_policy`
to references. When `allow_secret_references` is false, every reference is
invalid. When true, each reference must use an allowed scheme. The scheme map is:
`${VAR}`, `$VAR`, `%VAR%`, and `env:VAR` -> `env`; `<placeholder>` ->
`placeholder`; `{{template}}` -> `template`; and `secret://`, `vault://`,
`keyring://`, `aws-sm://`, `gcp-sm://`, or `azure-kv://` -> the URI scheme before
`://`. Diagnostics identify only category and file, never the value.

Portable package components must be strict UTF-8, NFC, and contain no Unicode
control-category character. Governed absolute-path lint covers POSIX roots,
forward and backslash UNC paths, drive roots, and file URLs while excluding
ordinary `http://` and `https://` URLs. Profile URLs are checked before parsing:
they must contain no raw whitespace/control characters and must be absolute
HTTP(S) URLs without userinfo.
