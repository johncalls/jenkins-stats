# Jenkins contract fixtures

These fixtures are sanitized JSON responses for the Jenkins API contract supported
by `JenkinsClient`.

## `sanitized_lts_pipeline`

Source configuration exercised by the fixture set:

- Jenkins LTS `2.492.3`
- Pipeline job (`workflow-job` `1540.v295eccc9778f`)
- Pipeline REST API plugin `2.38`
- Folder and multibranch-style nesting (`cloudbees-folder` `6.955.v81e2a_35c08d3`)

Sanitization replaced the controller origin with `https://jenkins.example/`,
renamed folders/jobs/branches to generic names, and removed user, parameter,
SCM, node, and log data. No credentials, crumb values, console text, parameters,
or environment variables are present.

The build fixture intentionally has `timestamp` earlier than
`wfapi/describe.startTimeMillis`. The client treats Jenkins core `timestamp` as
scheduled/queue time and obtains actual executor start time only from the
Pipeline REST API. The expected `end_time` is therefore
`startTimeMillis + duration` and is checked against `wfapi/describe.endTimeMillis`.
Non-Pipeline builds are not approximated: the corresponding fixture must raise
`StartTimeUnavailable` instead of inventing a start time from `timestamp`.

## Capturing or refreshing fixture data

Use `tools/capture_jenkins_contract_fixture.py` from this checkout to capture a
new sanitized fixture set from a controlled Jenkins instance. This is
repository-only test maintenance tooling and is not installed with the package.
The tool uses read-only GET requests through `HttpJsonTransport`; credentials
are read from environment variables and are never accepted as command-line
arguments.

```bash
JENKINS_USER=api-user JENKINS_TOKEN=api-token \
uv run python tools/capture_jenkins_contract_fixture.py \
  --base-url https://jenkins.internal.example/ \
  --out tests/fixtures/jenkins_contracts/new_lts_pipeline \
  --jenkins-version "2.492.3 LTS" \
  --plugin workflow-job=1540.v295eccc9778f \
  --plugin pipeline-rest-api=2.38 \
  --plugin cloudbees-folder=6.955.v81e2a_35c08d3 \
  --item folder_api=https://jenkins.internal.example/job/folder/ \
  --item multibranch_api=https://jenkins.internal.example/job/team-service/ \
  --item pipeline_job_item_api=https://jenkins.internal.example/job/folder/job/pipeline-main/ \
  --build-page pipeline_builds_page_0_2=https://jenkins.internal.example/job/folder/job/pipeline-main/,0,2 \
  --timing pipeline_build_42_wfapi_describe=https://jenkins.internal.example/job/folder/job/pipeline-main/42/ \
  --forbid internal.example
```

Useful options:

- `--dry-run` logs every request without contacting Jenkins.
- `--force` allows writing into an existing non-empty fixture directory.
- `--allow-insecure` permits HTTP for local throwaway Jenkins instances.
- `--forbid TEXT` fails the capture if generated JSON still contains `TEXT`;
  repeat it for hostnames, organization names, usernames, or other sensitive
  markers that must not be committed.

The root controller item is always captured as `root_api.json`. `--item` captures
an item, folder, multibranch project, or job with the same item tree used by
`JenkinsClient.iter_jobs()`. `--build-page NAME=JOB_URL,OFFSET,LIMIT` captures a
retained-build page with the same build fields used by
`JenkinsClient.iter_builds()`. `--timing NAME=BUILD_URL` captures the Pipeline
REST API `wfapi/describe` timing response for a specific build.

After capture, review every generated file before committing. The tool replaces
the real controller origin with `https://jenkins.example/`, drops common sensitive
fields such as actions, causes, parameters, environment data, change sets,
artifacts, descriptions, and console text, and scans the output for the real base
URL, credentials, and any `--forbid` values. Still manually verify that no job
names, branch names, SCM details, node names, parameters, logs, or organization
identifiers remain. Rename jobs/branches to generic names if necessary and update
`index.json` expected values plus the fixture-driven tests to describe the exact
Jenkins/plugin versions and timing semantics exercised.
