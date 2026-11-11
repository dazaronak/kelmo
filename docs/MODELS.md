# Embedding model selection

`sentence-transformers/all-MiniLM-L6-v2` is the default: an 87 MB download that needs no GPU.
Other models improve search, most of all when your notes mix languages.

## Measured results

398 questions over 199 notes (114 English, 85 Russian), each asked in both languages, on one private
vault of 9,530 text chunks. A hit means the right note is in the top 5 of the hybrid search (vector and
lexical results merged with equal weight). A model wrote the questions after reading the notes, so
expect lower numbers on your own questions. The vault is private, so these results cannot be
reproduced from this repository.

| Model    | One language | English and Russian together |
| -------- | ------------ | ---------------------------- |
| MiniLM   | 172 (43%)    | 288 (72%)                    |
| e5-small | 233 (59%)    | 352 (88%)                    |
| bge-m3   | 298 (75%)    | 350 (88%)                    |

By direction, hits out of the notes in that language:

| Question → note   | MiniLM | e5-small |
| ----------------- | ------ | -------- |
| English → English | 96/114 | 103/114  |
| Russian → Russian | 43/85  | 77/85    |
| English → Russian | 19/85  | 24/85    |
| Russian → English | 14/114 | 29/114   |

Measured on an AMD Ryzen 7 6800HS laptop with an RTX 3060 Laptop (6 GB), in a container limited to
8 CPU cores and 6 GB of RAM:

| Model    | Indexing, GPU | Added VRAM   | One query, GPU | Two-language query pair, CPU | CPU process memory |
| -------- | ------------- | ------------ | -------------- | ---------------------------- | ------------------ |
| MiniLM   | 612 chunks/s  | 0.9-1.2 GB   | 1.6 ms         | 7.3 ms                       | 683 MiB            |
| e5-small | 128 chunks/s  | not measured | not measured   | not measured                 | not measured       |
| bge-m3   | 24 chunks/s   | 2.8 GB       | 15 ms          | about 74 ms                  | 1.5-1.9 GB         |

CPU figures used 8 threads. Kelmo's local loader uses two, so expect slower queries there. On 8 CPU
threads MiniLM indexed 38 chunks per second.

e5-small and bge-m3 are not in the FastEmbed catalog, so they run through an
[embedding broker](EMBEDDING-BROKER.md). The catalog's own multilingual options,
`paraphrase-multilingual-MiniLM-L12-v2` (220 MB) and `multilingual-e5-large` (2.24 GB), were not
measured. Check the catalog with `kelmo models list --filter multilingual --json`.

## Inspect the catalog

Inspect the installed backend catalog and the vault's current selection:

```sh
kelmo models list --filter multilingual --json
kelmo models status --vault /path/to/vault --json
```

Listing models does not download weights or open a vault. It reports the backend's
model names, dimensions, sizes and licenses. Runtime embedding loads local files
only.

## Select local files

Import the bundled pinned model, then use the exact `model_dir` returned by import:

```sh
kelmo models import --vault /path/to/vault --from /path/to/model-cache --json
kelmo models use sentence-transformers/all-MiniLM-L6-v2 --vault /path/to/vault --from /path/to/vault/.cache/embeddings/model
```

For another supported model, select its complete provisioned directory and
declare the transformations its export uses:

```sh
kelmo models use MODEL_NAME --vault /path/to/vault --from /path/to/export --pooling mean --normalization l2 --dry-run
```

Use the export's actual pooling and normalization values. Pooling can be inferred
when `1_Pooling/config.json` declares exactly one supported mode; normalization
must be explicit for an unpinned model. The dimension comes from the backend
catalog unless supplied with `--dimension`. `--projection`, `--query-prefix` and
`--document-prefix` describe the remaining transforms.

The command hashes every regular artifact file and checks the selected model's
graph, tokenizer, dimension and declared input policy. Links and environment
files are refused. Unknown model names require a supported backend model or a
broker profile. No model code from the selected directory is executed.

Remove `--dry-run` to save the selection. Configuration writes preserve unrelated
TOML, detect concurrent changes and return a verified backup path. Repeating an
unchanged selection does not create another backup.

## Complete profiles and brokers

For a prepared local or brokered configuration:

```sh
kelmo models use --vault /path/to/vault --profile /path/to/embedding.json --dry-run
```

The JSON object has the same fields as the `[embedding]` table in the
[adoption contract](ADOPTION-CONTRACT.md). Relative paths are resolved against
the vault, not the profile file's directory. Broker configuration is validated
without opening its channel; the deployment launcher supplies that channel at
runtime. Custom spaces retain their model, tokenizer and transformation identity.
The [broker launcher](EMBEDDING-BROKER.md) binds a custom stdio executable to one
Kelmo command without installing a separate background connection.

`kelmo doctor` probes the selected embedding provider with one synthetic query.
When an explicit profile is configured, a successful default record model does
not hide that provider's failure. A broker must have its launcher-supplied
channel available for this check.

After applying a changed selection, restart the daemon so its cores start with
the new model (`systemctl --user restart kelmod.service`, or `launchctl
kickstart -k gui/$UID/org.kelmo.daemon`; a server runs the same service), then run `kelmo admin reembed --vault /path/to/vault`. Selection reports
`reindex_required`; it does not start a large reindex implicitly or combine
vectors from different spaces. Local FastEmbed loading uses two explicit CPU
threads so ONNX Runtime does not infer an affinity layout from a container host.
