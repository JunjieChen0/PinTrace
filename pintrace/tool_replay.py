"""Replay historical official tool modules against an isolated upstream fixture.

No repository is contacted or updated by either tool. Only transport/data-source
boundaries are substituted; extraction, version selection and update rules are
the pinned official implementations. This is a module replay, not hosted-bot PR
scheduling or an estimate of behavior across all historical deployments.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from pintrace.github import GitHub, github_token

RENOVATE_VERSION = "39.200.0"
RENOVATE_SHA = "476afca3d44459e4614976934834ee1664dbbee5"
DEPENDABOT_SHA = "18e9c1274ede47c78ed532eee072a1bd105167bb"


def prepare(directory):
    """Install isolated runtimes and pinned official packages; no global writes."""
    directory.mkdir(parents=True, exist_ok=True)
    downloads, commands = [], []

    def download(url, destination, expected=None):
        if not destination.exists():
            response = requests.get(url, timeout=(15, 90))
            response.raise_for_status()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(response.content)
        digest = sha256(destination)
        if expected and digest != expected:
            raise ValueError("Runtime archive checksum mismatch: " + destination.name)
        downloads.append({"url": url, "path": str(destination), "sha256": digest})
        return destination

    def command(args, name, env=None):
        completed = subprocess.run(args, cwd=directory, env=env, capture_output=True, timeout=600)
        (directory / ("prepare-" + name + ".stdout.log")).write_bytes(completed.stdout)
        (directory / ("prepare-" + name + ".stderr.log")).write_bytes(completed.stderr)
        commands.append({"command": args, "returncode": completed.returncode})
        if completed.returncode:
            raise RuntimeError("Environment preparation failed: " + name)

    runtimes = directory / "runtimes"
    node_dir = runtimes / "node/node-v22.14.0-win-x64"
    if not (node_dir / "node.exe").exists():
        package = download("https://nodejs.org/dist/v22.14.0/node-v22.14.0-win-x64.zip",
                           runtimes / "node-v22.14.0-win-x64.zip",
                           "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217")
        with zipfile.ZipFile(package) as archive:
            for name in archive.namelist():
                if not (runtimes / "node" / name).resolve().is_relative_to((runtimes / "node").resolve()):
                    raise ValueError("Unexpected archive member path")
            archive.extractall(runtimes / "node")
    ruby_dir = runtimes / "ruby/rubyinstaller-3.3.7-1-x64"
    if not (ruby_dir / "bin/ruby.exe").exists():
        package = download("https://github.com/oneclick/rubyinstaller2/releases/download/RubyInstaller-3.3.7-1/rubyinstaller-3.3.7-1-x64.7z",
                           runtimes / "rubyinstaller-3.3.7-1-x64.7z",
                           "7226277791cee0aa052cbbcd49da36deb172ddff9545f798c91078394d148cc3")
        extractor = shutil.which("7z") or str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe")
        command([extractor, "x", str(package), "-o" + str(runtimes / "ruby"), "-y"], "ruby-extract")
    if not (directory / "node/node_modules/renovate/package.json").exists():
        env = {**os.environ, "PATH": str(node_dir) + os.pathsep + os.environ.get("PATH", "")}
        command([str(node_dir / "node.exe"), str(node_dir / "node_modules/npm/bin/npm-cli.js"),
                 "install", "--prefix", str(directory / "node"), "--ignore-scripts", "--omit=optional",
                 "--no-audit", "--no-fund", "--save-exact", "renovate@" + RENOVATE_VERSION, "nock@13.5.6"], "npm", env)
    source_package = download("https://codeload.github.com/dependabot/dependabot-core/tar.gz/" + DEPENDABOT_SHA,
                              directory / "dependabot-source.tar.gz")
    source_dir = directory / "dependabot"
    if not (source_dir / "Gemfile.lock").exists():
        with tarfile.open(source_package) as archive:
            for member in archive:
                relative = "/".join(member.name.split("/")[1:])
                if not member.isfile() or not (relative.startswith(("common/lib/", "github_actions/lib/")) or relative in ("Gemfile.lock", ".ruby-version", "LICENSE")):
                    continue
                target = source_dir / relative
                if not target.resolve().is_relative_to(source_dir.resolve()):
                    raise ValueError("Unexpected archive member path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    pin_path = directory / "pinned-gems.json"
    if not pin_path.exists():
        versions = dict(re.findall(r"^    ([\w-]+) \(([^ )]+)\)", (source_dir / "Gemfile.lock").read_text(), re.M))
        names = ["public_suffix", "addressable", "sawyer", "faraday", "faraday-net_http", "multi_xml", "httparty",
                 "unicode-display_width", "terminal-table", "csv", "excon", "octokit", "gitlab", "sorbet-runtime",
                 "faraday-retry", "mini_mime"]
        pins = {name: versions[name] for name in names}
        packages = [str(download(f"https://rubygems.org/downloads/{name}-{version}.gem",
                                 directory / "gem-downloads" / f"{name}-{version}.gem")) for name, version in pins.items()]
        env = {**os.environ, "GEM_HOME": str(directory / "gems"), "GEM_PATH": str(directory / "gems")}
        command([str(ruby_dir / "bin/ruby.exe"), str(ruby_dir / "bin/gem"), "install", "--local", "--ignore-dependencies",
                 "--no-document", *packages], "gems", env)
        pin_path.write_text(json.dumps(pins, indent=2), encoding="utf-8")
    (directory / "preparation_manifest.json").write_text(json.dumps({"downloads": downloads, "commands": commands}, indent=2), encoding="utf-8")

RENOVATE_RUNNER = r'''
const fs = require('node:fs');
const path = require('node:path');
const root = process.argv[2];
const input = JSON.parse(fs.readFileSync(path.join(root,'inputs.json'),'utf8'));
const base = path.join(root,'node/node_modules/renovate/dist');
const nock = require(path.join(root,'node/node_modules/nock'));
nock.disableNetConnect();
const {GlobalConfig} = require(base+'/config/global');
GlobalConfig.set({platform:'github',endpoint:'https://api.github.com/'});
const {Result} = require(base+'/util/result');
const ds = require(base+'/modules/datasource');
const transportLog = [];
ds.getRawPkgReleases = config => {
  transportLog.push({boundary:'getRawPkgReleases',packageName:config.packageName});
  return Result.ok({sourceUrl:'https://github.com/actions/checkout', releases:
    Object.entries(input.upstream.tags).map(([version,newDigest])=>({version,newDigest}))});
};
const {GithubHttp} = require(base+'/util/http/github');
GithubHttp.prototype.getJsonUnchecked = async function(url) {
  transportLog.push({boundary:'GithubHttp.getJsonUnchecked',url});
  if (url === 'https://api.github.com/repos/actions/checkout/commits?per_page=1')
    return {body:[{sha:input.upstream.head}]};
  throw new Error('Unexpected network request: '+url);
};
const {extractPackageFile} = require(base+'/modules/manager/github-actions/extract');
const {fetchUpdates} = require(base+'/workers/repository/process/fetch');
const defaults = require(base+'/config/defaults');
(async()=>{
  const results=[];
  for(const item of input.cases) {
    const extracted = extractPackageFile(item.workflow,'.github/workflows/ci.yml');
    const files={'github-actions':[{packageFile:'.github/workflows/ci.yml',...structuredClone(extracted)}]};
    const config={...defaults.getConfig(),platform:'github',baseBranch:'main',
      repoIsOnboarded:true,packageRules:[{matchManagers:['github-actions'],enabled:item.covered}],
      'github-actions':{enabled:true}};
    const begin=transportLog.length;
    await fetchUpdates(config,files);
    results.push({case_id:item.case_id,covered:item.covered,extracted,
      official_result:files['github-actions'][0].deps,
      transport_calls:transportLog.slice(begin)});
  }
  console.log(JSON.stringify({tool:'renovate',version:require(path.join(root,'node/node_modules/renovate/package.json')).version,
    runtime:process.version,results,transportLog},null,2));
})().catch(err=>{console.error(err.stack);process.exitCode=1;});
'''

DEPENDABOT_RUNNER = r'''
require "rubygems"
pin_file = File.join(ARGV.fetch(0),"pinned-gems.json")
if File.exist?(pin_file)
  require "json"
  JSON.parse(File.read(pin_file)).each { |name,version| gem name,version }
end
require "json"
require "date"
require "uri"
root = ARGV.fetch(0)
$LOAD_PATH.unshift(File.join(root,"dependabot/common/lib"))
$LOAD_PATH.unshift(File.join(root,"dependabot/github_actions/lib"))
require "dependabot"
require "dependabot/source"
require "dependabot/dependency_file"
require "dependabot/dependency_group"
require "dependabot/config/file"
require "dependabot/config/ignore_condition"
require "dependabot/metadata_finders"
require "dependabot/shared_helpers"
require "dependabot/git_commit_checker"
require "dependabot/github_actions/file_parser"
require "dependabot/github_actions/update_checker"
require "dependabot/github_actions/file_updater"
input = JSON.parse(File.read(File.join(root,"inputs.json")))
transport_calls = []
upload_pack = input.fetch("upstream").fetch("upload_pack")
Dependabot::GitMetadataFetcher.class_eval do
  define_method(:fetch_raw_upload_pack_for) do |uri|
    transport_calls << {boundary:"fetch_raw_upload_pack_for",uri:uri}
    Excon::Response.new(status:200,body:upload_pack)
  end
end
results = input.fetch("cases").map do |item|
  config = Dependabot::Config::File.parse(item.fetch("dependabot_config"))
  # Hosted scheduling is outside dependabot-core. The fixture routes only jobs
  # explicitly present in the configuration parsed by the official Config class.
  covered = config.updates.any? { |u| u[:"package-ecosystem"] == "github-actions" && u[:directory] == "/" }
  file = Dependabot::DependencyFile.new(name:".github/workflows/ci.yml",content:item.fetch("workflow"),directory:"/")
  parser = Dependabot::GithubActions::FileParser.new(dependency_files:[file],
    source:Dependabot::Source.new(provider:"github",repo:"pintrace/offline-fixture"),credentials:[])
  begin_index = transport_calls.length
  dependencies = parser.parse
  row = {case_id:item.fetch("case_id"),covered:covered,
    extracted:dependencies.map { |d| {name:d.name,version:d.version,requirements:d.requirements} }}
  if covered
    row[:official_result] = dependencies.map do |dependency|
      checker = Dependabot::GithubActions::UpdateChecker.new(dependency:dependency,dependency_files:[file],credentials:[])
      latest = checker.latest_version
      requirements = checker.updated_requirements
      result = {name:dependency.name,latest_version:latest&.to_s,updated_requirements:requirements,up_to_date:checker.up_to_date?}
      if requirements != dependency.requirements
        updated = Dependabot::Dependency.new(name:dependency.name,version:latest&.to_s,
          requirements:requirements,previous_requirements:dependency.requirements,
          previous_version:dependency.version,package_manager:dependency.package_manager)
        updater = Dependabot::GithubActions::FileUpdater.new(dependencies:[updated],dependency_files:[file],credentials:[])
        result[:updated_files] = updater.updated_dependency_files.map { |f| {name:f.name,content:f.content} }
      end
      result
    end
  else
    row[:official_result] = nil
    row[:routing_status] = "fixture_did_not_schedule_missing_ecosystem"
  end
  row[:transport_calls] = transport_calls[begin_index..]
  row
end
puts JSON.pretty_generate(tool:"dependabot-core",version:Dependabot::VERSION,runtime:RUBY_DESCRIPTION,
  results:results,transport_calls:transport_calls,loaded_features:$LOADED_FEATURES.select { |f| f.include?("dependabot") },
  gems:Gem.loaded_specs.transform_values { |s| s.version.to_s })
'''


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_fixture(directory):
    """Construct four dated commit objects without changing the project index."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "fixture.json"
    if manifest.exists():
        return json.loads(manifest.read_text(encoding="utf-8"))
    env = {**os.environ, "GIT_AUTHOR_NAME": "PinTrace fixture", "GIT_COMMITTER_NAME": "PinTrace fixture",
           "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
           "GIT_AUTHOR_DATE": "2025-03-01T00:00:00Z", "GIT_COMMITTER_DATE": "2025-03-01T00:00:00Z"}

    def git(*args, content=None):
        result = subprocess.run(["git", "-C", str(directory), *args], input=content,
                                capture_output=True, check=True, env=env, timeout=30)
        return result.stdout.decode().strip()

    git("init", "--initial-branch=main")
    parent, commits = None, []
    for index in range(4):
        blob = git("hash-object", "-w", "--stdin", content=f"fixture stage {index}\n".encode())
        tree = git("mktree", content=f"100644 blob {blob}\tfixture.txt\n".encode())
        args = ["commit-tree", tree, "-m", f"fixture stage {index}"]
        if parent:
            args += ["-p", parent]
        parent = git(*args)
        commits.append(parent)
    git("update-ref", "refs/heads/main", commits[3])
    git("update-ref", "refs/tags/v4.2.2", commits[0])
    git("update-ref", "refs/tags/v4.3.1", commits[2])
    result = {"repository": "actions/checkout", "tags": {"v4.2.2": commits[0], "v4.3.1": commits[2]},
              "untagged": commits[1], "head": commits[3], "upload_pack": git("ls-remote", str(directory.resolve())) + "\n",
              "fixture_repo": directory.resolve().as_posix(), "synthetic_mapping": True}
    manifest.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def cases(upstream):
    output = []
    for kind in ["sha_with_version_comment", "tag_equivalent_bare_sha", "untagged_bare_sha"]:
        ref = upstream["untagged"] if kind == "untagged_bare_sha" else upstream["tags"]["v4.2.2"]
        suffix = " # v4.2.2" if kind == "sha_with_version_comment" else ""
        workflow = "name: offline replay\non: push\njobs:\n  test:\n    runs-on: self-hosted\n    steps:\n      - uses: actions/checkout@" + ref + suffix + "\n"
        for covered in [True, False]:
            ecosystem = "github-actions" if covered else "pip"
            output.append({"case_id": kind + ("__covered" if covered else "__not_covered"),
                           "kind": kind, "covered": covered, "workflow": workflow,
                           "renovate_config": {"packageRules": [{"matchManagers": ["github-actions"], "enabled": covered}]},
                           "dependabot_config": f"version: 2\nupdates:\n  - package-ecosystem: {ecosystem}\n    directory: /\n    schedule:\n      interval: daily\n"})
    return output


def execute(command, directory, name, env=None):
    attempt = directory / "attempts" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + name)
    attempt.mkdir(parents=True)
    try:
        completed = subprocess.run(command, cwd=directory, env=env, capture_output=True, timeout=180)
        stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout, stderr, returncode = error.stdout or b"", error.stderr or b"", "timeout_180s"
    for target in [directory, attempt]:
        (target / (name + ".stdout.log")).write_bytes(stdout)
        (target / (name + ".stderr.log")).write_bytes(stderr)
    record = {"command": command, "returncode": returncode, "attempt_directory": str(attempt),
              "stdout_sha256": sha256(attempt / (name + ".stdout.log")),
              "stderr_sha256": sha256(attempt / (name + ".stderr.log"))}
    if env:
        record["environment"] = {key: env[key] for key in ["GEM_HOME", "GEM_PATH", "TMPDIR", "GIT_CONFIG_COUNT",
                                                            "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"] if key in env}
    (directory / (name + ".command.json")).write_text(json.dumps(record, indent=2), encoding="utf-8")
    (attempt / (name + ".command.json")).write_text(json.dumps(record, indent=2), encoding="utf-8")
    if returncode == 0:
        return json.loads(stdout), record
    return None, record


def validate_results(results, inputs):
    """Check paired-run completeness and actual engine/routing outputs."""
    expected = {row["case_id"] for row in inputs["cases"]}
    failures = []
    for tool, result in results.items():
        if result is None:
            failures.append(tool + ": process failed")
            continue
        if {row["case_id"] for row in result["results"]} != expected or len(result["results"]) != 6:
            failures.append(tool + ": incomplete case coverage")
        for row in result["results"]:
            if tool == "renovate":
                for dependency in row["official_result"]:
                    if dependency.get("skipReason") == "internal-error":
                        failures.append(row["case_id"] + ": official engine internal error")
                    if not row["covered"] and (dependency.get("updates") or dependency.get("skipReason") != "disabled"):
                        failures.append(row["case_id"] + ": disabled configuration did not take effect")
                if row["covered"] and not any(dependency.get("updates") for dependency in row["official_result"]):
                    failures.append(row["case_id"] + ": no update decision")
            elif row["covered"] and not row["official_result"]:
                failures.append(row["case_id"] + ": missing official update decision")
    return failures


def verify_official_files(directory, metadata, loaded_sources):
    """Compare executed module bytes with the official source/package archives."""
    package = directory / f"renovate-{RENOVATE_VERSION}.tgz"
    if not package.exists():
        response = requests.get(metadata["dist"]["tarball"], timeout=(15, 45))
        response.raise_for_status()
        package.write_bytes(response.content)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(package.read_bytes()).digest()).decode()
    if integrity != metadata["dist"]["integrity"]:
        raise ValueError("Official npm package integrity mismatch")
    failures, checked = [], 0
    with tarfile.open(directory / "dependabot-source.tar.gz") as archive:
        prefix = archive.getmembers()[0].name.split("/")[0] + "/"
        for local, expected in loaded_sources.items():
            if not local.startswith("dependabot/"):
                continue
            member = archive.getmember(prefix + local[len("dependabot/"):])
            actual = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            checked += 1
            if actual != expected:
                failures.append(local)
    with tarfile.open(package) as archive:
        for local, expected in loaded_sources.items():
            prefix = "node/node_modules/renovate/"
            if not local.startswith(prefix):
                continue
            member = archive.getmember("package/" + local[len(prefix):])
            actual = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            checked += 1
            if actual != expected:
                failures.append(local)
    return {"checked_executed_sources": checked, "files_match_official_archives": not failures,
            "mismatched_files": failures, "npm_integrity_verified": True,
            "npm_tarball_sha256": sha256(package)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--prepare", action="store_true", help="Prepare isolated Windows runtimes and tool packages first")
    args = parser.parse_args()
    root = args.root.resolve()
    directory = root / "data/tool-replay"
    if args.prepare:
        prepare(directory)
    upstream = git_fixture(directory / "upstream-fixture")
    payload = {"upstream": upstream, "cases": cases(upstream)}
    (directory / "inputs.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (directory / "renovate-runner.cjs").write_text(RENOVATE_RUNNER, encoding="utf-8")
    (directory / "dependabot-runner.rb").write_text(DEPENDABOT_RUNNER, encoding="utf-8")
    metadata_path = directory / "renovate-package-metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        response = requests.get(f"https://registry.npmjs.org/renovate/{RENOVATE_VERSION}", timeout=(15, 45))
        response.raise_for_status()
        metadata = response.json()
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if metadata["gitHead"] != RENOVATE_SHA:
        raise ValueError("Pinned Renovate package commit mismatch")
    node = directory / "runtimes/node/node-v22.14.0-win-x64/node.exe"
    ruby_root = directory / "runtimes/ruby/rubyinstaller-3.3.7-1-x64"
    ruby = ruby_root / "bin/ruby.exe"
    node_result, node_command = execute([str(node), str(directory / "renovate-runner.cjs"), str(directory)], directory, "renovate")
    tmp = directory / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {**os.environ, "GEM_HOME": str(directory / "gems"), "GEM_PATH": str(directory / "gems"),
           "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
           "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "url." + (directory / "upstream-fixture").as_uri() + ".insteadOf",
           "GIT_CONFIG_VALUE_0": "https://github.com/actions/checkout"}
    ruby_result, ruby_command = execute([str(ruby), str(directory / "dependabot-runner.rb"), str(directory)], directory, "dependabot", env)
    results = {"renovate": node_result, "dependabot": ruby_result}
    checks = validate_results(results, payload)
    for name, result in results.items():
        if result is not None:
            (directory / (name + "-results.json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    client = GitHub(root / "data/cache", github_token())
    source_metadata = {}
    for tool, repo, commit in [("renovate", "renovatebot/renovate", RENOVATE_SHA),
                               ("dependabot", "dependabot/dependabot-core", DEPENDABOT_SHA)]:
        entry = client.api(f"repos/{repo}/commits/{commit}")
        source_metadata[tool] = {"repository": repo, "commit": commit, "committed_at": entry["commit"]["committer"]["date"],
                                 "source_url": entry["html_url"]}
    loaded_source_hashes = {}
    if ruby_result:
        for path in ruby_result["loaded_features"]:
            if Path(path).is_file():
                loaded_source_hashes[str(Path(path).relative_to(directory)).replace("\\", "/")] = sha256(path)
    renovate_modules = ["modules/manager/github-actions/extract.js", "modules/datasource/github-tags/index.js",
                        "workers/repository/process/fetch.js", "workers/repository/process/lookup/index.js"]
    for module in renovate_modules:
        path = directory / "node/node_modules/renovate/dist" / module
        loaded_source_hashes[str(path.relative_to(directory)).replace("\\", "/")] = sha256(path)
    archive_check = verify_official_files(directory, metadata, loaded_source_hashes)
    if not archive_check["files_match_official_archives"]:
        checks.append("Executed source files differ from official archives")
    report = {"rule_version": "tool-replay-v1", "checked_at": datetime.now(timezone.utc).isoformat(),
              "implementation_sha256": sha256(__file__),
              "runtime_archive_hashes": {path.name: sha256(path) for path in [directory / "runtimes/node-v22.14.0-win-x64.zip",
                                                                            directory / "runtimes/rubyinstaller-3.3.7-1-x64.7z"]},
              "input_sha256": sha256(directory / "inputs.json"),
              "checks_passed": not checks, "check_failures": checks,
              "source_metadata": source_metadata, "loaded_source_hashes": loaded_source_hashes,
              "official_archive_check": archive_check,
              "synthetic_upstream_mapping": True,
              "tools": {"renovate": {"version": RENOVATE_VERSION, "commit": RENOVATE_SHA, "command": node_command,
                                      "completed": node_result is not None},
                        "dependabot": {"version": "0.301.0", "commit": DEPENDABOT_SHA, "command": ruby_command,
                                        "completed": ruby_result is not None}},
              "files": {str(path.relative_to(directory)).replace("\\", "/"): sha256(path)
                        for path in [directory / "renovate-runner.cjs", directory / "dependabot-runner.rb",
                                     directory / "node/package-lock.json", directory / "dependabot-source.tar.gz",
                                     directory / "pinned-gems.json", metadata_path]},
              "results": results,
              "limits": ["Official extraction/update modules, not the hosted bot scheduler or PR creation.",
                         "Upstream mapping is synthetic and shared between tools; it is not historical availability evidence.",
                         "Only transport/data-source boundaries are mocked. Selection and rewriting use official code.",
                         "Dependabot missing-ecosystem routing is a fixture gate, not a replay of GitHub's private scheduler.",
                         "Absent local Renovate configuration does not prove that global/inherited automation was absent."]}
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports/tool_replay_results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"tools": {name: record["completed"] for name, record in report["tools"].items()},
                      "checks_passed": not checks, "check_failures": checks}))


if __name__ == "__main__":
    main()
