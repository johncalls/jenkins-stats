# jenkins-stats

Tools for collecting and analyzing Jenkins build statistics. The package includes typed Pydantic domain models, a read-only Jenkins JSON client, and SQLite persistence for collected jobs and builds.

## Usage

From this checkout, run the installed console script through `uv`:

```bash
uv run jenkins-stats --help
```

The CLI has one top-level workflow:

```text
jenkins-stats collect [connection options] {all,jobs,builds} [target options]
```

Connection options must appear before the collection target. They can be passed
as flags or environment variables:

```bash
export JENKINS_URL=https://jenkins.example.com/
export JENKINS_USERNAME=api-user
export JENKINS_API_TOKEN=api-token

uv run jenkins-stats collect --db ./jenkins.sqlite all --lookback 6h
```

Common connection options:

- `--url` / `--jenkins-url` or `JENKINS_URL`
- `--username` or `JENKINS_USERNAME`
- `--api-token` or `JENKINS_API_TOKEN`
- `--db` / `--database` (default: `./jenkins.sqlite`)
- `--timeout` in seconds (default: `30`)

### `collect all`

Discover visible jobs and collect builds for the jobs that support build
collection:

```bash
uv run jenkins-stats collect all
uv run jenkins-stats collect all --lookback 6h
```

### `collect jobs`

Discover visible jobs only. This updates stored job metadata and deletion state,
but does not request build endpoints:

```bash
uv run jenkins-stats collect jobs
```

### `collect builds`

Collect builds for jobs already present in SQLite. This target has its own
subcommands:

```text
jenkins-stats collect [connection options] builds {job,all-jobs} [build options]
```

#### `collect builds job`

Collect builds for one stored job. `JOB_FULL_NAME` is Jenkins' full job name,
including folder path segments such as `folder/deploy-main`:

```bash
uv run jenkins-stats collect builds job "folder/deploy-main" --since 2024-01-01T00:00:00Z
```

#### `collect builds all-jobs`

Collect builds for every active job already stored in SQLite, without
rediscovering jobs from Jenkins:

```bash
uv run jenkins-stats collect builds all-jobs --lookback 6h
```

### Build options

`collect all`, `collect builds job`, and `collect builds all-jobs` accept:

- `--lookback DURATION`, using `ms`, `s`, `m`, `h`, or `d` units; bare numbers
  are seconds.
- `--since DATETIME`, an inclusive completion-time cutoff. It must be a
  timezone-aware ISO timestamp; `Z` is accepted for UTC.
- `--page-size N`, the Jenkins retained-build page size (default: `100`).

`--since` and `--lookback` are mutually exclusive. Omit both to import every
retained build visible through `allBuilds`. Use `--since` when you need a fixed
cutoff shared across a multi-job run; relative lookbacks are resolved while jobs
are being scanned.

Job discovery compares the successful Jenkins traversal with jobs already in
SQLite: visible jobs are stored as active, and previously stored jobs that are no
longer visible are marked deleted with the observation time. Visible jobs also
store Jenkins disabled status; disabled jobs are skipped for build collection.
Stored-job build collection only considers active (not deleted) jobs. If a later
`collect jobs` or `collect all` sees the same full name after it was marked
deleted, collection fails because SQLite rejects resetting `deleted_at`; resolve
the reintroduced job manually so old build history is not silently merged with a
new job lifetime. Successful build-collection runs print the active completion
filter after the stored-build summary.

### Operational prerequisites

Before running against a controller, confirm that:

- the Jenkins base URL is the HTTPS controller URL, including any context path,
  with no embedded credentials, query, or fragment. The CLI has no insecure-HTTP
  flag; plain HTTP is only available to library users who explicitly construct
  `HttpJsonTransport(..., allow_insecure=True)` for trusted local/test use;
- the controller/proxy serves JSON API responses directly. Redirects, including
  login redirects, are rejected before the redirect target is opened;
- the API user can authenticate with a username and API token and has read
  visibility for the controller root, folders/containers, job `api/json` build
  metadata, and Pipeline `wfapi/describe` timing endpoints. Hidden jobs are not
  returned, and permission failures abort collection. The client only uses GET
  requests, so no Jenkins CSRF crumb is required;
- build collection currently supports Pipeline jobs whose job class is
  `org.jenkinsci.plugins.workflow.job.WorkflowJob`; actual executor start time is
  read from the Pipeline REST API plugin. Other, missing, or unknown job classes
  are skipped and reported in the collection summary. The job class is stored and
  can be refreshed with `collect jobs`;
- the Pipeline REST API plugin is installed and each collected Pipeline run has a
  readable `wfapi/describe` endpoint with `startTimeMillis`;
- the SQLite path is writable and scoped to one Jenkins controller/base URL. The
  current schema does not isolate multiple controllers, so reusing the same
  database for another controller can mix or overwrite rows with matching job
  names and build numbers;
- SQLite migrations add job and build class columns to existing databases. Jobs
  already stored before that migration have an unknown class until rediscovered
  with `collect jobs` or `collect all`;
- collection is fail-loud for HTTP errors, redirects, missing Pipeline timing
  for a supported run, invalid Jenkins payloads, and storage failures. Unsupported
  job classes are skipped and reported rather than treated as collection failures.
  If the CLI fails after writes begin, the SQLite database may contain earlier
  job or build writes; fix the cause and rerun the command to upsert safely;
- Jenkins retention policies limit what can be collected. Deleted or no-longer
  retained builds are unavailable to the client and are not automatically marked
  deleted in SQLite; and
- pagination is not a transactional snapshot of Jenkins history. Concurrent build
  completion or deletion can change what later pages return.

Completion-time cutoffs are local filters. Jenkins is still asked for retained
history pages, and terminal Pipeline builds need timing requests before their
completion time can be checked, so a small lookback is not a server-side request
limit.

## Development

This project is managed by [uv](https://docs.astral.sh/uv/), uses [just](https://just.systems/) for common tasks, and targets Python 3.14.

```bash
just test    # run pytest
just lint    # run formatting, linting, and type checks
just format  # format Python code
just check   # run lint and test
just clean   # remove non-project files (except anything in .jj)
```

When adding or changing a `Protocol`, a real implementation of a protocol, or a
test fake intended to satisfy a protocol, update
`tests/test_protocol_conformance.py` so mypy/pyright explicitly verify that
implementation against the intended interface.
