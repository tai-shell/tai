/* extract.c — Pull the appended payload out of the binary into a
   cache dir on first run. Cache layout:

       ~/Library/Caches/tai/
           payload-<48 hex chars>/      ← cache dir, keyed by trailer
              .stamp                    ← created last; signals "ready"
              version
              runtime/
              holo/
              site-packages/
              sikuli/

   The 48 hex chars come from the trailer's raw bytes — see
   `cache_key_from_trailer`. A different build (different bash,
   different bundle contents) produces a different trailer and gets
   a different cache dir, so upgrades silently provision new caches
   and old caches stay intact (rollback is a `cp` away). */

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "extract.h"

/* Block SIGCHLD across our fork+waitpid pairs.

   Why: when extraction runs from a `tai`-named invocation, the lazy
   Py_Initialize path fires from inside a holo builtin AFTER bash has
   installed its sigchld_handler (via initialize_job_control). That
   handler calls waitpid(-1, WNOHANG) on every SIGCHLD and harvests
   ANY child it doesn't recognize. If it fires in the window between
   our fork returning and our specific waitpid(pid), bash reaps our
   tar/rm child first and our waitpid returns -1/ECHILD, surfacing
   as a spurious "tar -xz failed" / "waitpid failed" error.

   Fix: block SIGCHLD around each fork+waitpid pair. Children
   restore the original mask before exec so the spawned process
   isn't started with an unusual signal mask. */
static int
tai_block_sigchld (sigset_t *oldmask_out)
{
  sigset_t blockmask;
  sigemptyset (&blockmask);
  sigaddset (&blockmask, SIGCHLD);
  return sigprocmask (SIG_BLOCK, &blockmask, oldmask_out);
}

static void
tai_restore_sigmask (const sigset_t *oldmask)
{
  sigprocmask (SIG_SETMASK, oldmask, NULL);
}

/* Render the 24-byte trailer as 48 lowercase hex characters into
   out (which must hold >= 49 bytes including the terminator). */
static void
cache_key_from_trailer (const tai_payload_trailer_t *trailer, char *out)
{
  const unsigned char *bytes = (const unsigned char *) trailer;
  static const char hex[] = "0123456789abcdef";
  for (size_t i = 0; i < sizeof *trailer; i++)
    {
      out[2 * i] = hex[bytes[i] >> 4];
      out[2 * i + 1] = hex[bytes[i] & 0xF];
    }
  out[2 * sizeof *trailer] = '\0';
}

/* mkdir -p equivalent. Returns 0 on success (or already exists). */
static int
mkdir_p (const char *path)
{
  char buf[PATH_MAX];
  strncpy (buf, path, sizeof buf - 1);
  buf[sizeof buf - 1] = '\0';
  for (char *p = buf + 1; *p; p++)
    {
      if (*p == '/')
	{
	  *p = '\0';
	  if (mkdir (buf, 0755) != 0 && errno != EEXIST)
	    return -1;
	  *p = '/';
	}
    }
  if (mkdir (buf, 0755) != 0 && errno != EEXIST)
    return -1;
  return 0;
}

/* Sweep stale `payload-<key>.tmp-<pid>` dirs in `cache_root` whose
   pids are no longer alive. Each first-launch extraction creates a
   per-pid temp dir; on a crash the dir is orphaned and never picked
   up by a future run (which gets a different pid). Without this
   sweep, ~/Library/Caches/tai/ accumulates them. We probe each
   suspected pid with kill(pid, 0): ESRCH means the process is
   gone; any other result means it might still be a concurrent
   extractor and we leave it alone.

   PID reuse can mask a stale dir behind a live unrelated process,
   but the next sweep (when that unrelated process exits) catches
   it. Acceptable for a janitor. */
static void rm_rf (const char *path);    /* forward decl */
static void
sweep_stale_tmp_dirs (const char *cache_root, const char *key)
{
  DIR *d = opendir (cache_root);
  if (d == NULL)
    return;
  char prefix[NAME_MAX];
  int plen = snprintf (prefix, sizeof prefix, "payload-%s.tmp-", key);
  if (plen <= 0 || (size_t) plen >= sizeof prefix)
    {
      closedir (d);
      return;
    }

  struct dirent *e;
  while ((e = readdir (d)) != NULL)
    {
      if (strncmp (e->d_name, prefix, (size_t) plen) != 0)
	continue;
      const char *pid_str = e->d_name + plen;
      char *endptr = NULL;
      errno = 0;
      long pid = strtol (pid_str, &endptr, 10);
      if (errno != 0 || endptr == pid_str || *endptr != '\0' || pid <= 0)
	continue;

      /* kill(pid, 0) returns 0 if the process exists (any uid), or
	 -1 with errno=ESRCH if it doesn't. EPERM means it exists but
	 we can't signal it — treat as live to be safe. */
      if (kill ((pid_t) pid, 0) == 0)
	continue;
      if (errno != ESRCH)
	continue;

      char path[PATH_MAX];
      snprintf (path, sizeof path, "%s/%s", cache_root, e->d_name);
      rm_rf (path);
    }
  closedir (d);
}

/* Recursively remove a directory tree. Used on extraction failure to
   not leave a half-populated temp dir behind. Simple shell-out — the
   number of files is bounded and rm is portable. */
static void
rm_rf (const char *path)
{
  sigset_t oldmask;
  tai_block_sigchld (&oldmask);
  pid_t pid = fork ();
  if (pid < 0)
    {
      /* fork() exhausted process slots or RLIMIT_NPROC. The temp dir
	 we were asked to clean up will linger until the next run's
	 sweep_stale_tmp_dirs picks it up. Better than silently
	 deadlocking the parent. */
      fprintf (stderr, "tai: rm_rf: fork() failed: %s\n",
	       strerror (errno));
      tai_restore_sigmask (&oldmask);
      return;
    }
  if (pid == 0)
    {
      /* Restore the original signal mask before exec so the child
	 doesn't start with SIGCHLD blocked. */
      tai_restore_sigmask (&oldmask);
      execlp ("rm", "rm", "-rf", path, (char *) NULL);
      _exit (127);
    }
  waitpid (pid, NULL, 0);
  tai_restore_sigmask (&oldmask);
}

/* Stream `length` bytes from `src_fd` (already positioned) into the
   pipe whose write-end is `pipe_w`. Returns 0 on success.

   EINTR retry: extraction can take hundreds of ms on a cold cache;
   any signal that fires during the loop (SIGWINCH from a terminal
   resize, SIGUSR1, SIGCHLD slipping through if our block is somehow
   defeated) would otherwise abort with -1 and the caller would
   surface "tar -xz failed" for what is really a benign retry. */
static int
stream_payload_to_pipe (int src_fd, int pipe_w, off_t length)
{
  unsigned char buf[64 * 1024];
  off_t remaining = length;
  while (remaining > 0)
    {
      size_t want = remaining > (off_t) sizeof buf
		    ? sizeof buf : (size_t) remaining;
      ssize_t n;
      do
	{
	  n = read (src_fd, buf, want);
	}
      while (n < 0 && errno == EINTR);
      if (n <= 0)
	return -1;
      ssize_t off = 0;
      while (off < n)
	{
	  ssize_t w;
	  do
	    {
	      w = write (pipe_w, buf + off, (size_t) (n - off));
	    }
	  while (w < 0 && errno == EINTR);
	  if (w <= 0)
	    return -1;
	  off += w;
	}
      remaining -= n;
    }
  return 0;
}

/* Spawn `tar -xz -C dest_dir`, return its stdin write-fd in *pipe_w
   and its pid in *child_pid. The caller writes payload bytes to
   *pipe_w, then closes it, then waitpid()s on *child_pid.

   Blocks SIGCHLD before forking and stashes the old mask in
   *oldmask_out so a peer sigchld_handler (bash's, in the user-shell
   path) can't reap our tar child first. Caller MUST restore the
   mask via tai_restore_sigmask(oldmask_out) after its waitpid. */
static int
spawn_tar (const char *dest_dir, int *pipe_w, pid_t *child_pid,
	   sigset_t *oldmask_out)
{
  int fds[2];
  if (pipe (fds) != 0)
    return -1;
  if (tai_block_sigchld (oldmask_out) != 0)
    {
      close (fds[0]);
      close (fds[1]);
      return -1;
    }
  pid_t pid = fork ();
  if (pid < 0)
    {
      tai_restore_sigmask (oldmask_out);
      close (fds[0]);
      close (fds[1]);
      return -1;
    }
  if (pid == 0)
    {
      /* child — restore the original mask before exec so tar runs
	 with normal signal semantics. */
      tai_restore_sigmask (oldmask_out);
      if (dup2 (fds[0], STDIN_FILENO) < 0)
	_exit (127);
      close (fds[0]);
      close (fds[1]);
      execlp ("tar", "tar", "-xz", "-C", dest_dir, (char *) NULL);
      _exit (127);
    }
  close (fds[0]);
  *pipe_w = fds[1];
  *child_pid = pid;
  return 0;
}

int
tai_payload_ensure_extracted (const char *exe_path,
			      const tai_payload_trailer_t *trailer,
			      char *cache_dir_out)
{
  /* Compose cache paths. */
  const char *home = getenv ("HOME");
  if (home == NULL)
    {
      fprintf (stderr, "tai: $HOME is not set; can't locate cache dir\n");
      return -1;
    }

  char cache_root[PATH_MAX];
  snprintf (cache_root, sizeof cache_root,
	    "%s/Library/Caches/tai", home);

  char key[49];
  cache_key_from_trailer (trailer, key);

  char cache_dir[PATH_MAX];
  snprintf (cache_dir, sizeof cache_dir,
	    "%s/payload-%s", cache_root, key);

  char stamp[PATH_MAX];
  snprintf (stamp, sizeof stamp, "%s/.stamp", cache_dir);

  /* Fast path: stamp present → already extracted, nothing to do. */
  struct stat st;
  if (stat (stamp, &st) == 0)
    {
      strncpy (cache_dir_out, cache_dir, PATH_MAX - 1);
      cache_dir_out[PATH_MAX - 1] = '\0';
      return 0;
    }

  /* Slow path: extract into a temp dir, then atomically rename. The
     pid in the temp-dir name guarantees that two simultaneous first-
     launches don't trample each other; whichever finishes first wins
     the rename, the loser cleans up its temp dir. */
  if (mkdir_p (cache_root) != 0)
    {
      fprintf (stderr, "tai: failed to create cache root %s: %s\n",
	       cache_root, strerror (errno));
      return -1;
    }

  /* Sweep orphaned temp dirs from prior crashed runs (any pid that's
     no longer alive). Bounded — typically zero, occasionally one. */
  sweep_stale_tmp_dirs (cache_root, key);

  char tmp_dir[PATH_MAX];
  snprintf (tmp_dir, sizeof tmp_dir,
	    "%s.tmp-%d", cache_dir, (int) getpid ());
  if (mkdir (tmp_dir, 0755) != 0)
    {
      fprintf (stderr, "tai: mkdir %s failed: %s\n",
	       tmp_dir, strerror (errno));
      return -1;
    }

  fprintf (stderr, "tai: extracting bundled payload to %s\n", cache_dir);

  int src_fd = open (exe_path, O_RDONLY);
  if (src_fd < 0)
    {
      fprintf (stderr, "tai: open(%s): %s\n",
	       exe_path, strerror (errno));
      rm_rf (tmp_dir);
      return -1;
    }
  if (lseek (src_fd, (off_t) trailer->offset, SEEK_SET) < 0)
    {
      fprintf (stderr, "tai: lseek to payload failed: %s\n",
	       strerror (errno));
      close (src_fd);
      rm_rf (tmp_dir);
      return -1;
    }

  int pipe_w;
  pid_t child;
  sigset_t spawn_oldmask;
  if (spawn_tar (tmp_dir, &pipe_w, &child, &spawn_oldmask) != 0)
    {
      fprintf (stderr, "tai: failed to spawn tar\n");
      close (src_fd);
      rm_rf (tmp_dir);
      return -1;
    }

  int rc = stream_payload_to_pipe (src_fd, pipe_w, (off_t) trailer->length);
  close (pipe_w);
  close (src_fd);

  /* SIGCHLD stays blocked from spawn_tar through this waitpid so a
     peer sigchld_handler (bash's, in the user-shell path) can't reap
     `child` first and turn our waitpid into ECHILD. */
  int status;
  int waitpid_rc = waitpid (child, &status, 0);
  tai_restore_sigmask (&spawn_oldmask);
  if (waitpid_rc < 0)
    {
      fprintf (stderr, "tai: waitpid on tar failed: %s\n",
	       strerror (errno));
      rm_rf (tmp_dir);
      return -1;
    }
  if (rc != 0 || !WIFEXITED (status) || WEXITSTATUS (status) != 0)
    {
      fprintf (stderr, "tai: tar -xz failed (status=%d)\n", status);
      rm_rf (tmp_dir);
      return -1;
    }

  /* Stamp file last — its presence is the "extraction complete"
     signal. Writing into tmp_dir (which we still own exclusively),
     then renaming the whole dir, gives atomic readiness from the
     fast-path's perspective. */
  char tmp_stamp[PATH_MAX];
  snprintf (tmp_stamp, sizeof tmp_stamp, "%s/.stamp", tmp_dir);
  FILE *sf = fopen (tmp_stamp, "w");
  if (sf == NULL)
    {
      fprintf (stderr, "tai: stamp write failed: %s\n", strerror (errno));
      rm_rf (tmp_dir);
      return -1;
    }
  fprintf (sf, "ready\n");
  fclose (sf);

  /* Atomic rename to the final cache dir. If a concurrent extractor
     beat us to it, the rename fails with EEXIST/ENOTEMPTY on macOS
     because rename(dir, existing_dir) only succeeds if the target
     is empty; treat that as a benign race and clean up our temp. */
  if (rename (tmp_dir, cache_dir) != 0)
    {
      if (errno == EEXIST || errno == ENOTEMPTY)
	{
	  /* Lost the race — peer extractor populated cache_dir. */
	  rm_rf (tmp_dir);
	}
      else
	{
	  fprintf (stderr, "tai: rename %s -> %s failed: %s\n",
		   tmp_dir, cache_dir, strerror (errno));
	  rm_rf (tmp_dir);
	  return -1;
	}
    }

  strncpy (cache_dir_out, cache_dir, PATH_MAX - 1);
  cache_dir_out[PATH_MAX - 1] = '\0';
  return 0;
}
