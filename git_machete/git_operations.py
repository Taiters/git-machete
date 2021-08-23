from typing import Callable, Dict, Generator, Iterator, List, Match, Optional, Tuple, Set

import os
import re
import sys

from git_machete.options import CommandLineOptions
from git_machete.exceptions import MacheteException
from git_machete.utils import colored, debug, fmt
from git_machete import utils
from git_machete.constants import AHEAD_OF_REMOTE, BEHIND_REMOTE, DIVERGED_FROM_AND_NEWER_THAN_REMOTE, \
    DIVERGED_FROM_AND_OLDER_THAN_REMOTE, IN_SYNC_WITH_REMOTE, \
    NO_REMOTES, MAX_COUNT_FOR_INITIAL_LOG, UNTRACKED, YELLOW


REFLOG_ENTRY = Tuple[str, str]


class GitContext:

    def __init__(self, cli_opts: CommandLineOptions) -> None:
        self.cli_opts: CommandLineOptions = cli_opts
        self.git_version: Optional[Tuple[int, ...]] = None
        self.root_dir: Optional[str] = None
        self.git_dir: Optional[str] = None
        self.__fetch_done_for: Set[str] = set()
        self.__config_cached: Optional[Dict[str, str]] = None
        self.__remotes_cached: Optional[List[str]] = None
        self.__counterparts_for_fetching_cached: Optional[Dict[str, Optional[str]]] = None  # TODO (#110): default dict with None
        self.__short_commit_sha_by_revision_cached: Dict[str, str] = {}
        self.__tree_sha_by_commit_sha_cached: Optional[Dict[str, Optional[str]]] = None  # TODO (#110): default dict with None
        self.__commit_sha_by_revision_cached: Optional[Dict[str, Optional[str]]] = None  # TODO (#110): default dict with None
        self.__committer_unix_timestamp_by_revision_cached: Optional[Dict[str, int]] = None  # TODO (#110): default dict with 0
        self.__local_branches_cached: Optional[List[str]] = None
        self.__remote_branches_cached: Optional[List[str]] = None
        self.__initial_log_shas_cached: Dict[str, List[str]] = {}
        self.__remaining_log_shas_cached: Dict[str, List[str]] = {}
        self.__reflogs_cached: Optional[Dict[str, Optional[List[REFLOG_ENTRY]]]] = None
        self.__merge_base_cached: Dict[Tuple[str, str], str] = {}
        self.__contains_equivalent_tree_cached: Dict[Tuple[str, str], bool] = {}

    @staticmethod
    def run_git(git_cmd: str, *args: str, **kwargs: Dict[str, str]) -> int:
        exit_code = utils.run_cmd("git", git_cmd, *args, **kwargs)
        if not kwargs.get("allow_non_zero") and exit_code != 0:
            raise MacheteException(f"`{utils.cmd_shell_repr('git', git_cmd, *args, **kwargs)}` returned {exit_code}")
        return exit_code

    @staticmethod
    def popen_git(git_cmd: str, *args: str, **kwargs: Dict[str, str]) -> str:
        exit_code, stdout, stderr = utils.popen_cmd("git", git_cmd, *args, **kwargs)
        if not kwargs.get("allow_non_zero") and exit_code != 0:
            exit_code_msg: str = fmt(f"`{utils.cmd_shell_repr('git', git_cmd, *args, **kwargs)}` returned {exit_code}\n")
            stdout_msg: str = f"\n{utils.bold('stdout')}:\n{utils.dim(stdout)}" if stdout else ""
            stderr_msg: str = f"\n{utils.bold('stderr')}:\n{utils.dim(stderr)}" if stderr else ""
            # Not applying the formatter to avoid transforming whatever characters might be in the output of the command.
            raise MacheteException(exit_code_msg + stdout_msg + stderr_msg, apply_fmt=False)
        return stdout

    def get_default_editor(self) -> Optional[str]:
        # Based on the git's own algorithm for identifying the editor.
        # '$GIT_MACHETE_EDITOR', 'editor' (to please Debian-based systems) and 'nano' have been added.
        git_machete_editor_var = "GIT_MACHETE_EDITOR"
        proposed_editor_funs: List[Tuple[str, Callable[[], Optional[str]]]] = [
            ("$" + git_machete_editor_var, lambda: os.environ.get(git_machete_editor_var)),
            ("$GIT_EDITOR", lambda: os.environ.get("GIT_EDITOR")),
            ("git config core.editor", lambda: self.get_config_or_none("core.editor")),
            ("$VISUAL", lambda: os.environ.get("VISUAL")),
            ("$EDITOR", lambda: os.environ.get("EDITOR")),
            ("editor", lambda: "editor"),
            ("nano", lambda: "nano"),
            ("vi", lambda: "vi"),
        ]

        for name, fun in proposed_editor_funs:
            editor = fun()
            if not editor:
                debug("get_default_editor()", f"'{name}' is undefined")
            else:
                editor_repr = f"'{name}'{(' (' + editor + ')') if editor != name else ''}"
                if not utils.find_executable(editor):
                    debug("get_default_editor()", f"{editor_repr} is not available")
                    if name == "$" + git_machete_editor_var:
                        # In this specific case, when GIT_MACHETE_EDITOR is defined but doesn't point to a valid executable,
                        # it's more reasonable/less confusing to raise an error and exit without opening anything.
                        raise MacheteException(f"<b>{editor_repr}</b> is not available")
                else:
                    debug("get_default_editor()", f"{editor_repr} is available")
                    if name != "$" + git_machete_editor_var and self.get_config_or_none('advice.macheteEditorSelection') != 'false':
                        sample_alternative = 'nano' if editor.startswith('vi') else 'vi'
                        sys.stderr.write(
                            fmt(f"Opening <b>{editor_repr}</b>.\n",
                                f"To override this choice, use <b>{git_machete_editor_var}</b> env var, e.g. `export {git_machete_editor_var}={sample_alternative}`.\n\n",
                                "See `git machete help edit` and `git machete edit --debug` for more details.\n\nUse `git config --global advice.macheteEditorSelection false` to suppress this message.\n"))
                    return editor

        # This case is extremely unlikely on a modern Unix-like system.
        return None

    def get_git_version(self) -> Tuple[int, ...]:
        if not self.git_version:
            # We need to cut out the x.y.z part and not just take the result of 'git version' as is,
            # because the version string in certain distributions of git (esp. on OS X) has an extra suffix,
            # which is irrelevant for our purpose (checking whether certain git CLI features are available/bugs are fixed).
            raw = re.search(r"\d+.\d+.\d+", self.popen_git("version")).group(0)
            self.git_version = tuple(map(int, raw.split(".")))
        return self.git_version

    def get_root_dir(self) -> str:
        if not self.root_dir:
            try:
                self.root_dir = self.popen_git("rev-parse", "--show-toplevel").strip()
            except MacheteException:
                raise MacheteException("Not a git repository")
        return self.root_dir

    def __get_git_dir(self) -> str:
        if not self.git_dir:
            try:
                self.git_dir = self.popen_git("rev-parse", "--git-dir").strip()
            except MacheteException:
                raise MacheteException("Not a git repository")
        return self.git_dir

    def get_git_subpath(self, *fragments: str) -> str:
        return os.path.join(self.__get_git_dir(), *fragments)

    def parse_git_timespec_to_unix_timestamp(self, date: str) -> int:
        try:
            return int(self.popen_git("rev-parse", "--since=" + date).replace("--max-age=", "").strip())
        except (MacheteException, ValueError):
            raise MacheteException(f"Cannot parse timespec: `{date}`")

    def __ensure_config_loaded(self) -> None:
        if self.__config_cached is None:
            self.__config_cached = {}
            for config_line in utils.non_empty_lines(self.popen_git("config", "--list")):
                k_v = config_line.split("=", 1)
                if len(k_v) == 2:
                    k, v = k_v
                    self.__config_cached[k.lower()] = v

    def get_config_or_none(self, key: str) -> Optional[str]:
        self.__ensure_config_loaded()
        return self.__config_cached.get(key.lower())

    def set_config(self, key: str, value: str) -> None:
        self.run_git("config", "--", key, value)
        self.__ensure_config_loaded()
        self.__config_cached[key.lower()] = value

    def unset_config(self, key: str) -> None:
        self.__ensure_config_loaded()
        if self.get_config_or_none(key):
            self.run_git("config", "--unset", key)
            del self.__config_cached[key.lower()]

    def remotes(self) -> List[str]:
        if self.__remotes_cached is None:
            self.__remotes_cached = utils.non_empty_lines(self.popen_git("remote"))
        return self.__remotes_cached

    def get_url_of_remote(self, remote: str) -> str:
        return self.popen_git("remote", "get-url", "--", remote).strip()

    def fetch_remote(self, remote: str) -> None:
        if remote not in self.__fetch_done_for:
            self.run_git("fetch", remote)
            self.__fetch_done_for.add(remote)

    def set_upstream_to(self, rb: str) -> None:
        self.run_git("branch", "--set-upstream-to", rb)

    def reset_keep(self, to_revision: str) -> None:
        try:
            self.run_git("reset", "--keep", to_revision)
        except MacheteException:
            raise MacheteException(
                f"Cannot perform `git reset --keep {to_revision}`. This is most likely caused by local uncommitted changes.")

    def push(self, remote: str, b: str, force_with_lease: bool = False) -> None:
        if not force_with_lease:
            opt_force = []
        elif self.get_git_version() >= (1, 8, 5):  # earliest version of git to support 'push --force-with-lease'
            opt_force = ["--force-with-lease"]
        else:
            opt_force = ["--force"]
        args = [remote, b]
        self.run_git("push", "--set-upstream", *(opt_force + args))

    def pull_ff_only(self, remote: str, rb: str) -> None:
        self.fetch_remote(remote)
        self.run_git("merge", "--ff-only", rb)
        # There's apparently no way to set remote automatically when doing 'git pull' (as opposed to 'git push'),
        # so a separate 'git branch --set-upstream-to' is needed.
        self.set_upstream_to(rb)

    def __find_short_commit_sha_by_revision(self, revision: str) -> str:
        return self.popen_git("rev-parse", "--short", revision + "^{commit}").rstrip()

    def short_commit_sha_by_revision(self, revision: str) -> str:
        if revision not in self.__short_commit_sha_by_revision_cached:
            self.__short_commit_sha_by_revision_cached[revision] = self.__find_short_commit_sha_by_revision(revision)
        return self.__short_commit_sha_by_revision_cached[revision]

    def __find_commit_sha_by_revision(self, revision: str) -> Optional[str]:
        # Without ^{commit}, 'git rev-parse --verify' will not only accept references to other kinds of objects (like trees and blobs),
        # but just echo the argument (and exit successfully) even if the argument doesn't match anything in the object store.
        try:
            return self.popen_git("rev-parse", "--verify", "--quiet", revision + "^{commit}").rstrip()
        except MacheteException:
            return None

    def commit_sha_by_revision(self, revision: str, prefix: str = "refs/heads/") -> Optional[str]:
        if self.__commit_sha_by_revision_cached is None:
            self.__load_branches()
        full_revision: str = prefix + revision
        if full_revision not in self.__commit_sha_by_revision_cached:
            self.__commit_sha_by_revision_cached[full_revision] = self.__find_commit_sha_by_revision(full_revision)
        return self.__commit_sha_by_revision_cached[full_revision]

    def __find_tree_sha_by_revision(self, revision: str) -> Optional[str]:
        try:
            return self.popen_git("rev-parse", "--verify", "--quiet", revision + "^{tree}").rstrip()
        except MacheteException:
            return None

    def tree_sha_by_commit_sha(self, commit_sha: str) -> Optional[str]:
        if self.__tree_sha_by_commit_sha_cached is None:
            self.__load_branches()
        if commit_sha not in self.__tree_sha_by_commit_sha_cached:
            self.__tree_sha_by_commit_sha_cached[commit_sha] = self.__find_tree_sha_by_revision(commit_sha)
        return self.__tree_sha_by_commit_sha_cached[commit_sha]

    @staticmethod
    def is_full_sha(revision: str) -> Optional[Match[str]]:
        return re.match("^[0-9a-f]{40}$", revision)

    # Resolve a revision identifier to a full sha
    def full_sha(self, revision: str, prefix: str = "refs/heads/") -> Optional[str]:
        if prefix == "" and self.is_full_sha(revision):
            return revision
        else:
            return self.commit_sha_by_revision(revision, prefix)

    def committer_unix_timestamp_by_revision(self, revision: str, prefix: str = "refs/heads/") -> int:
        if self.__committer_unix_timestamp_by_revision_cached is None:
            self.__load_branches()
        return self.__committer_unix_timestamp_by_revision_cached.get(prefix + revision, 0)

    def inferred_remote_for_fetching_of_branch(self, b: str) -> Optional[str]:
        # Since many people don't use '--set-upstream' flag of 'push', we try to infer the remote instead.
        for r in self.remotes():
            if f"{r}/{b}" in self.remote_branches():
                return r
        return None

    def strict_remote_for_fetching_of_branch(self, b: str) -> Optional[str]:
        remote = self.get_config_or_none(f"branch.{b}.remote")
        return remote.rstrip() if remote else None

    def combined_remote_for_fetching_of_branch(self, b: str) -> Optional[str]:
        return self.strict_remote_for_fetching_of_branch(b) or self.inferred_remote_for_fetching_of_branch(b)

    def __inferred_counterpart_for_fetching_of_branch(self, b: str) -> Optional[str]:
        for r in self.remotes():
            if f"{r}/{b}" in self.remote_branches():
                return f"{r}/{b}"
        return None

    def strict_counterpart_for_fetching_of_branch(self, b: str) -> Optional[str]:
        if self.__counterparts_for_fetching_cached is None:
            self.__load_branches()
        return self.__counterparts_for_fetching_cached.get(b)

    def combined_counterpart_for_fetching_of_branch(self, b: str) -> Optional[str]:
        # Since many people don't use '--set-upstream' flag of 'push' or 'branch', we try to infer the remote if the tracking data is missing.
        return self.strict_counterpart_for_fetching_of_branch(b) or self.__inferred_counterpart_for_fetching_of_branch(b)

    def is_am_in_progress(self) -> bool:
        # As of git 2.24.1, this is how 'cmd_rebase()' in builtin/rebase.c checks whether am is in progress.
        return os.path.isfile(self.get_git_subpath("rebase-apply", "applying"))

    def is_cherry_pick_in_progress(self) -> bool:
        return os.path.isfile(self.get_git_subpath("CHERRY_PICK_HEAD"))

    def is_merge_in_progress(self) -> bool:
        return os.path.isfile(self.get_git_subpath("MERGE_HEAD"))

    def is_revert_in_progress(self) -> bool:
        return os.path.isfile(self.get_git_subpath("REVERT_HEAD"))

    def checkout(self, branch: str) -> None:
        self.run_git("checkout", "--quiet", branch, "--")

    def local_branches(self) -> List[str]:
        if self.__local_branches_cached is None:
            self.__load_branches()
        return self.__local_branches_cached

    def remote_branches(self) -> List[str]:
        if self.__remote_branches_cached is None:
            self.__load_branches()
        return self.__remote_branches_cached

    def __load_branches(self) -> None:
        self.__commit_sha_by_revision_cached = {}
        self.__committer_unix_timestamp_by_revision_cached = {}
        self.__counterparts_for_fetching_cached = {}
        self.__local_branches_cached = []
        self.__remote_branches_cached = []
        self.__tree_sha_by_commit_sha_cached = {}

        # Using 'committerdate:raw' instead of 'committerdate:unix' since the latter isn't supported by some older versions of git.
        raw_remote = utils.non_empty_lines(self.popen_git("for-each-ref", "--format=%(refname)\t%(objectname)\t%(tree)\t%(committerdate:raw)", "refs/remotes"))
        for line in raw_remote:
            values = line.split("\t")
            if len(values) != 4:
                continue  # invalid, shouldn't happen
            b, commit_sha, tree_sha, committer_unix_timestamp_and_time_zone = values
            b_stripped = re.sub("^refs/remotes/", "", b)
            self.__remote_branches_cached += [b_stripped]
            self.__commit_sha_by_revision_cached[b] = commit_sha
            self.__tree_sha_by_commit_sha_cached[commit_sha] = tree_sha
            self.__committer_unix_timestamp_by_revision_cached[b] = int(committer_unix_timestamp_and_time_zone.split(' ')[0])

        raw_local = utils.non_empty_lines(self.popen_git("for-each-ref", "--format=%(refname)\t%(objectname)\t%(tree)\t%(committerdate:raw)\t%(upstream)", "refs/heads"))

        for line in raw_local:
            values = line.split("\t")
            if len(values) != 5:
                continue  # invalid, shouldn't happen
            b, commit_sha, tree_sha, committer_unix_timestamp_and_time_zone, fetch_counterpart = values
            b_stripped = re.sub("^refs/heads/", "", b)
            fetch_counterpart_stripped = re.sub("^refs/remotes/", "", fetch_counterpart)
            self.__local_branches_cached += [b_stripped]
            self.__commit_sha_by_revision_cached[b] = commit_sha
            self.__tree_sha_by_commit_sha_cached[commit_sha] = tree_sha
            self.__committer_unix_timestamp_by_revision_cached[b] = int(committer_unix_timestamp_and_time_zone.split(' ')[0])
            if fetch_counterpart_stripped in self.__remote_branches_cached:
                self.__counterparts_for_fetching_cached[b_stripped] = fetch_counterpart_stripped

    def __log_shas(self, revision: str, max_count: Optional[int]) -> List[str]:
        opts = ([f"--max-count={str(max_count)}"] if max_count else []) + ["--format=%H", f"refs/heads/{revision}"]
        return utils.non_empty_lines(self.popen_git("log", *opts))

    # Since getting the full history of a branch can be an expensive operation for large repositories (compared to all other underlying git operations),
    # there's a simple optimization in place: we first fetch only a couple of first commits in the history,
    # and only fetch the rest if needed.
    def spoonfeed_log_shas(self, b: str) -> Generator[str, None, None]:
        if b not in self.__initial_log_shas_cached:
            self.__initial_log_shas_cached[b] = self.__log_shas(b, max_count=MAX_COUNT_FOR_INITIAL_LOG)
        for sha in self.__initial_log_shas_cached[b]:
            yield sha

        if b not in self.__remaining_log_shas_cached:
            self.__remaining_log_shas_cached[b] = self.__log_shas(b, max_count=None)[MAX_COUNT_FOR_INITIAL_LOG:]
        for sha in self.__remaining_log_shas_cached[b]:
            yield sha

    def __load_all_reflogs(self) -> None:
        # %gd - reflog selector (refname@{num})
        # %H - full hash
        # %gs - reflog subject
        all_branches = [f"refs/heads/{b}" for b in self.local_branches()] + \
                       [f"refs/remotes/{self.combined_counterpart_for_fetching_of_branch(b)}" for b in self.local_branches() if self.combined_counterpart_for_fetching_of_branch(b)]
        # The trailing '--' is necessary to avoid ambiguity in case there is a file called just exactly like one of the branches.
        entries = utils.non_empty_lines(self.popen_git("reflog", "show", "--format=%gD\t%H\t%gs", *(all_branches + ["--"])))
        self.__reflogs_cached = {}
        for entry in entries:
            values = entry.split("\t")
            if len(values) != 3:  # invalid, shouldn't happen
                continue
            selector, sha, subject = values
            branch_and_pos = selector.split("@")
            if len(branch_and_pos) != 2:  # invalid, shouldn't happen
                continue
            b, pos = branch_and_pos
            if b not in self.__reflogs_cached:
                self.__reflogs_cached[b] = []
            self.__reflogs_cached[b] += [(sha, subject)]

    def reflog(self, b: str) -> List[REFLOG_ENTRY]:
        # git version 2.14.2 fixed a bug that caused fetching reflog of more than
        # one branch at the same time unreliable in certain cases
        if self.get_git_version() >= (2, 14, 2):
            if self.__reflogs_cached is None:
                self.__load_all_reflogs()
            return self.__reflogs_cached.get(b, [])
        else:
            if self.__reflogs_cached is None:
                self.__reflogs_cached = {}
            if b not in self.__reflogs_cached:
                # %H - full hash
                # %gs - reflog subject
                self.__reflogs_cached[b] = [
                    tuple(entry.split(":", 1)) for entry in utils.non_empty_lines(  # type: ignore
                        # The trailing '--' is necessary to avoid ambiguity in case there is a file called just exactly like the branch 'b'.
                        self.popen_git("reflog", "show", "--format=%H:%gs", b, "--"))
                ]
            return self.__reflogs_cached[b]

    def create_branch(self, b: str, out_of_revision: str) -> None:
        self.run_git("checkout", "-b", b, out_of_revision)
        self.flush_caches()  # the repository state has changed b/c of a successful branch creation, let's defensively flush all the caches

    def flush_caches(self) -> None:
        self.__commit_sha_by_revision_cached = None
        self.__config_cached = None
        self.__counterparts_for_fetching_cached = None
        self.__initial_log_shas_cached = {}
        self.__local_branches_cached = None
        self.__reflogs_cached = None
        self.__remaining_log_shas_cached = {}
        self.__remote_branches_cached = None

    def get_revision_repr(self, revision: str) -> str:
        short_sha = self.short_commit_sha_by_revision(revision)
        if self.is_full_sha(revision) or revision == short_sha:
            return f"commit {revision}"
        else:
            return f"{revision} (commit {self.short_commit_sha_by_revision(revision)})"

    # Note: while rebase is ongoing, the repository is always in a detached HEAD state,
    # so we need to extract the name of the currently rebased branch from the rebase-specific internals
    # rather than rely on 'git symbolic-ref HEAD' (i.e. the contents of .git/HEAD).
    def currently_rebased_branch_or_none(self) -> Optional[str]:  # utils/private
        # https://stackoverflow.com/questions/3921409

        head_name_file = None

        # .git/rebase-merge directory exists during cherry-pick-powered rebases,
        # e.g. all interactive ones and the ones where '--strategy=' or '--keep-empty' option has been passed
        rebase_merge_head_name_file = self.get_git_subpath("rebase-merge", "head-name")
        if os.path.isfile(rebase_merge_head_name_file):
            head_name_file = rebase_merge_head_name_file

        # .git/rebase-apply directory exists during the remaining, i.e. am-powered rebases, but also during am sessions.
        rebase_apply_head_name_file = self.get_git_subpath("rebase-apply", "head-name")
        # Most likely .git/rebase-apply/head-name can't exist during am sessions, but it's better to be safe.
        if not self.is_am_in_progress() and os.path.isfile(rebase_apply_head_name_file):
            head_name_file = rebase_apply_head_name_file

        if not head_name_file:
            return None
        with open(head_name_file) as f:
            raw = f.read().strip()
            return re.sub("^refs/heads/", "", raw)

    def currently_checked_out_branch_or_none(self) -> Optional[str]:
        try:
            raw = self.popen_git("symbolic-ref", "--quiet", "HEAD").strip()
            return re.sub("^refs/heads/", "", raw)
        except MacheteException:
            return None

    def expect_no_operation_in_progress(self) -> None:
        rb = self.currently_rebased_branch_or_none()
        if rb:
            raise MacheteException(
                f"Rebase of `{rb}` in progress. Conclude the rebase first with `git rebase --continue` or `git rebase --abort`.")
        if self.is_am_in_progress():
            raise MacheteException(
                "`git am` session in progress. Conclude `git am` first with `git am --continue` or `git am --abort`.")
        if self.is_cherry_pick_in_progress():
            raise MacheteException(
                "Cherry pick in progress. Conclude the cherry pick first with `git cherry-pick --continue` or `git cherry-pick --abort`.")
        if self.is_merge_in_progress():
            raise MacheteException(
                "Merge in progress. Conclude the merge first with `git merge --continue` or `git merge --abort`.")
        if self.is_revert_in_progress():
            raise MacheteException(
                "Revert in progress. Conclude the revert first with `git revert --continue` or `git revert --abort`.")

    def current_branch_or_none(self) -> Optional[str]:
        return self.currently_checked_out_branch_or_none() or self.currently_rebased_branch_or_none()

    def current_branch(self) -> str:
        result = self.current_branch_or_none()
        if not result:
            raise MacheteException("Not currently on any branch")
        return result

    def __merge_base(self, sha1: str, sha2: str) -> str:
        if sha1 > sha2:
            sha1, sha2 = sha2, sha1
        if not (sha1, sha2) in self.__merge_base_cached:
            # Note that we don't pass '--all' flag to 'merge-base', so we'll get only one merge-base
            # even if there is more than one (in the rare case of criss-cross histories).
            # This is still okay from the perspective of is-ancestor checks that are our sole use of merge-base:
            # * if any of sha1, sha2 is an ancestor of another,
            #   then there is exactly one merge-base - the ancestor,
            # * if neither of sha1, sha2 is an ancestor of another,
            #   then none of the (possibly more than one) merge-bases is equal to either of sha1/sha2 anyway.
            self.__merge_base_cached[sha1, sha2] = self.popen_git("merge-base", sha1, sha2).rstrip()
        return self.__merge_base_cached[sha1, sha2]

    # Note: the 'git rev-parse --verify' validation is not performed in case for either of earlier/later
    # if the corresponding prefix is empty AND the revision is a 40 hex digit hash.
    def is_ancestor_or_equal(
            self,
            earlier_revision: str,
            later_revision: str,
            earlier_prefix: str = "refs/heads/",
            later_prefix: str = "refs/heads/",
    ) -> bool:
        earlier_sha = self.full_sha(earlier_revision, earlier_prefix)
        later_sha = self.full_sha(later_revision, later_prefix)

        if earlier_sha == later_sha:
            return True
        return self.__merge_base(earlier_sha, later_sha) == earlier_sha

    # Determine if later_revision, or any ancestors of later_revision that are NOT ancestors of earlier_revision,
    # contain a tree with identical contents to earlier_revision, indicating that
    # later_revision contains a rebase or squash merge of earlier_revision.
    def contains_equivalent_tree(
            self,
            earlier_revision: str,
            later_revision: str,
            earlier_prefix: str = "refs/heads/",
            later_prefix: str = "refs/heads/",
    ) -> bool:
        earlier_commit_sha = self.full_sha(earlier_revision, earlier_prefix)
        later_commit_sha = self.full_sha(later_revision, later_prefix)

        if earlier_commit_sha == later_commit_sha:
            return True

        if (earlier_commit_sha, later_commit_sha) in self.__contains_equivalent_tree_cached:
            return self.__contains_equivalent_tree_cached[earlier_commit_sha, later_commit_sha]

        debug(
            "contains_equivalent_tree",
            f"earlier_revision={earlier_revision} later_revision={later_revision}",
        )

        earlier_tree_sha = self.tree_sha_by_commit_sha(earlier_commit_sha)

        # `git log later_commit_sha ^earlier_commit_sha`
        # shows all commits reachable from later_commit_sha but NOT from earlier_commit_sha
        intermediate_tree_shas = utils.non_empty_lines(
            self.popen_git(
                "log",
                "--format=%T",  # full commit's tree hash
                "^" + earlier_commit_sha,
                later_commit_sha
            )
        )

        result = earlier_tree_sha in intermediate_tree_shas
        self.__contains_equivalent_tree_cached[earlier_commit_sha, later_commit_sha] = result
        return result

    def get_sole_remote_branch(self, b: str) -> Optional[str]:
        def matches(rb: str) -> bool:
            # Note that this matcher is defensively too inclusive:
            # if there is both origin/foo and origin/feature/foo,
            # then both are matched for 'foo';
            # this is to reduce risk wrt. which '/'-separated fragments belong to remote and which to branch name.
            # FIXME (#116): this is still likely to deliver incorrect results in rare corner cases with compound remote names.
            return rb.endswith(f"/{b}")

        matching_remote_branches = list(filter(matches, self.remote_branches()))
        return matching_remote_branches[0] if len(matching_remote_branches) == 1 else None

    def merged_local_branches(self) -> List[str]:
        return list(map(
            lambda b: re.sub("^refs/heads/", "", b),
            utils.non_empty_lines(
                self.popen_git("for-each-ref", "--format=%(refname)", "--merged", "HEAD", "refs/heads"))
        ))

    def get_hook_path(self, hook_name: str) -> str:
        hook_dir: str = self.get_config_or_none("core.hooksPath") or self.get_git_subpath("hooks")
        return os.path.join(hook_dir, hook_name)

    def check_hook_executable(self, hook_path: str) -> bool:
        if not os.path.isfile(hook_path):
            return False
        elif not utils.is_executable(hook_path):
            advice_ignored_hook = self.get_config_or_none("advice.ignoredHook")
            if advice_ignored_hook != 'false':  # both empty and "true" is okay
                # The [33m color must be used to keep consistent with how git colors this advice for its built-in hooks.
                sys.stderr.write(
                    colored(f"hint: The '{hook_path}' hook was ignored because it's not set as executable.",
                            YELLOW) + "\n")
                sys.stderr.write(
                    colored("hint: You can disable this warning with `git config advice.ignoredHook false`.",
                            YELLOW) + "\n")
            return False
        else:
            return True

    def merge(self, branch: str, into: str) -> None:  # refs/heads/ prefix is assumed for 'branch'
        extra_params = ["--no-edit"] if self.cli_opts.opt_no_edit_merge else ["--edit"]
        # We need to specify the message explicitly to avoid 'refs/heads/' prefix getting into the message...
        commit_message = f"Merge branch '{branch}' into {into}"
        # ...since we prepend 'refs/heads/' to the merged branch name for unambiguity.
        self.run_git("merge", "-m", commit_message, f"refs/heads/{branch}", *extra_params)

    def merge_fast_forward_only(self, branch: str) -> None:  # refs/heads/ prefix is assumed for 'branch'
        self.run_git("merge", "--ff-only", f"refs/heads/{branch}")

    def rebase(self, onto: str, fork_commit: str, branch: str) -> None:
        def do_rebase() -> None:
            try:
                if self.cli_opts.opt_no_interactive_rebase:
                    self.run_git("rebase", "--onto", onto, fork_commit, branch)
                else:
                    self.run_git("rebase", "--interactive", "--onto", onto, fork_commit, branch)
            finally:
                # https://public-inbox.org/git/317468c6-40cc-9f26-8ee3-3392c3908efb@talktalk.net/T
                # In our case, this can happen when git version invoked by git-machete to start the rebase
                # is different than git version used (outside of git-machete) to continue the rebase.
                # This used to be the case when git-machete was installed via a strict-confinement snap
                # with its own version of git baked in as a dependency.
                # Currently we're using classic-confinement snaps which no longer have this problem
                # (snapped git-machete uses whatever git is available in the host system),
                # but it still doesn't harm to patch the author script.

                # No need to fix <git-dir>/rebase-apply/author-script,
                # only <git-dir>/rebase-merge/author-script (i.e. interactive rebases, for the most part) is affected.
                author_script = self.get_git_subpath("rebase-merge", "author-script")
                if os.path.isfile(author_script):
                    faulty_line_regex = re.compile("[A-Z0-9_]+='[^']*")

                    def fix_if_needed(line: str) -> str:
                        return f"{line.rstrip()}'\n" if faulty_line_regex.fullmatch(line) else line

                    def get_all_lines_fixed() -> Iterator[str]:
                        with open(author_script) as f_read:
                            return map(fix_if_needed, f_read.readlines())

                    fixed_lines = get_all_lines_fixed()  # must happen before the 'with' clause where we open for writing
                    with open(author_script, "w") as f_write:
                        f_write.write("".join(fixed_lines))

        hook_path = self.get_hook_path("machete-pre-rebase")
        if self.check_hook_executable(hook_path):
            debug(f"rebase({onto}, {fork_commit}, {branch})", f"running machete-pre-rebase hook ({hook_path})")
            exit_code = utils.run_cmd(hook_path, onto, fork_commit, branch, cwd=self.get_root_dir())
            if exit_code == 0:
                do_rebase()
            else:
                sys.stderr.write("The machete-pre-rebase hook refused to rebase.\n")
                sys.exit(exit_code)
        else:
            do_rebase()

    def rebase_onto_ancestor_commit(self, branch: str, ancestor_commit: str) -> None:
        self.rebase(ancestor_commit, ancestor_commit, branch)

    def commits_between(self, earliest_exclusive: str, latest_inclusive: str) -> List[Tuple[str, str, str]]:
        # Reverse the list, since `git log` by default returns the commits from the latest to earliest.
        return list(reversed(list(map(
            lambda x: tuple(x.split(":", 2)),  # type: ignore
            utils.non_empty_lines(
                self.popen_git("log", "--format=%H:%h:%s", f"^{earliest_exclusive}", latest_inclusive, "--"))
        ))))

    def get_relation_to_remote_counterpart(self, b: str, rb: str) -> int:
        b_is_anc_of_rb = self.is_ancestor_or_equal(b, rb, later_prefix="refs/remotes/")
        rb_is_anc_of_b = self.is_ancestor_or_equal(rb, b, earlier_prefix="refs/remotes/")
        if b_is_anc_of_rb:
            return IN_SYNC_WITH_REMOTE if rb_is_anc_of_b else BEHIND_REMOTE
        elif rb_is_anc_of_b:
            return AHEAD_OF_REMOTE
        else:
            b_t = self.committer_unix_timestamp_by_revision(b, "refs/heads/")
            rb_t = self.committer_unix_timestamp_by_revision(rb, "refs/remotes/")
            return DIVERGED_FROM_AND_OLDER_THAN_REMOTE if b_t < rb_t else DIVERGED_FROM_AND_NEWER_THAN_REMOTE

    def get_strict_remote_sync_status(self, b: str) -> Tuple[int, Optional[str]]:
        if not self.remotes():
            return NO_REMOTES, None
        rb = self.strict_counterpart_for_fetching_of_branch(b)
        if not rb:
            return UNTRACKED, None
        return self.get_relation_to_remote_counterpart(b, rb), self.strict_remote_for_fetching_of_branch(b)

    def get_combined_remote_sync_status(self, b: str) -> Tuple[int, Optional[str]]:
        if not self.remotes():
            return NO_REMOTES, None
        rb = self.combined_counterpart_for_fetching_of_branch(b)
        if not rb:
            return UNTRACKED, None
        return self.get_relation_to_remote_counterpart(b, rb), self.combined_remote_for_fetching_of_branch(b)

    def get_latest_checkout_timestamps(self) -> Dict[str, int]:  # TODO (#110): default dict with 0
        # Entries are in the format '<branch_name>@{<unix_timestamp> <time-zone>}'
        result = {}
        # %gd - reflog selector (HEAD@{<unix-timestamp> <time-zone>} for `--date=raw`;
        #   `--date=unix` is not available on some older versions of git)
        # %gs - reflog subject
        output = self.popen_git("reflog", "show", "--format=%gd:%gs", "--date=raw")
        for entry in utils.non_empty_lines(output):
            pattern = "^HEAD@\\{([0-9]+) .+\\}:checkout: moving from (.+) to (.+)$"
            match = re.search(pattern, entry)
            if match:
                from_branch = match.group(2)
                to_branch = match.group(3)
                # Only the latest occurrence for any given branch is interesting
                # (i.e. the first one to occur in reflog)
                if from_branch not in result:
                    result[from_branch] = int(match.group(1))
                if to_branch not in result:
                    result[to_branch] = int(match.group(1))
        return result