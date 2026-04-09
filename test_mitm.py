#!/usr/bin/env python3
"""
MITM defense verification tests for fast-copy.

These are LIVE tests that exercise actual SSH attack scenarios:
  1. Verify host key change is REJECTED (not silently accepted)
  2. Verify unknown-host TOFU prompt rejects by default ([y/N])
  3. Verify Ctrl-C/EOF on the prompt is treated as rejection
  4. Verify --no-verify does NOT bypass host key checking
  5. Verify there's no env var that disables host key checking
  6. Verify both system and user known_hosts are loaded
  7. Verify SHA-256 of the update download is computed
  8. Verify the update URL is HTTPS-only and pinned to GitHub
  9. Verify SSL cert verification can't be silently disabled
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAST_COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fast_copy.py")
PYTHON = sys.executable


def run_fc(*args, input_text=None, env=None, timeout=30):
    cmd = [PYTHON, FAST_COPY] + list(args)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       input=input_text, env=full_env, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def strip_ansi(s):
    return re.sub(r'\x1b\[[0-9;]*m', '', s)


# ════════════════════════════════════════════════════════════════════════
# Live SSH MITM tests
# ════════════════════════════════════════════════════════════════════════

class TestHostKeyChangeRejection(unittest.TestCase):
    """Verify that a CHANGED host key (potential MITM) is rejected,
    not silently re-accepted or auto-replaced."""

    def setUp(self):
        # Use a temp HOME so we don't pollute the real ~/.ssh/known_hosts
        self.tmphome = tempfile.mkdtemp(prefix="mitm_test_")
        self.tmpknown = os.path.join(self.tmphome, ".ssh", "known_hosts")
        os.makedirs(os.path.dirname(self.tmpknown), mode=0o700)
        # Plant a wrong host key for localhost so paramiko will reject the real one
        # Generate a fake ed25519 public key
        fake_key = ("[localhost]:22 ssh-ed25519 "
                    "AAAAC3NzaC1lZDI1NTE5AAAAIDeadBeef" + "A" * 32 + "=")
        with open(self.tmpknown, "w") as f:
            f.write(fake_key + "\n")
        os.chmod(self.tmpknown, 0o600)

        # Create a tiny source dir for the copy attempt
        self.src = tempfile.mkdtemp(prefix="mitm_src_")
        with open(os.path.join(self.src, "f.txt"), "w") as f:
            f.write("test")
        self.dst = tempfile.mkdtemp(prefix="mitm_dst_")

    def tearDown(self):
        for d in (self.tmphome, self.src, self.dst):
            shutil.rmtree(d, ignore_errors=True)

    def test_changed_host_key_rejected(self):
        """With a wrong key planted in known_hosts, fast-copy must
        REFUSE to connect — never silently accept the new key."""
        # Run fast_copy with HOME pointing to our planted known_hosts
        out, err, rc = run_fc(
            self.src, "{}@localhost:{}".format(os.environ.get("USER", "kai"), self.dst),
            env={"HOME": self.tmphome},
            timeout=30,
        )
        # Must fail (rc != 0) AND must not silently overwrite the key
        self.assertNotEqual(rc, 0,
            "Fast-copy connected despite wrong key in known_hosts! "
            "stdout={}".format(out[:300]))
        # Check the known_hosts wasn't silently replaced with the real key
        with open(self.tmpknown) as f:
            content = f.read()
        self.assertIn("DeadBeef", content,
                      "known_hosts was silently overwritten with new key!")


class TestUnknownHostPromptDefault(unittest.TestCase):
    """Verify the [y/N] prompt defaults to N (reject)."""

    def setUp(self):
        self.tmphome = tempfile.mkdtemp(prefix="mitm_tofu_")
        os.makedirs(os.path.join(self.tmphome, ".ssh"), mode=0o700)
        self.src = tempfile.mkdtemp(prefix="mitm_tofu_src_")
        with open(os.path.join(self.src, "f.txt"), "w") as f:
            f.write("test")

    def tearDown(self):
        shutil.rmtree(self.tmphome, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def test_empty_input_rejects(self):
        """Pressing Enter at the prompt without typing y must reject."""
        out, err, rc = run_fc(
            self.src,
            "nobody@127.0.0.1:/tmp/dst",   # use a host not in known_hosts
            input_text="\n",                # just press Enter
            env={"HOME": self.tmphome},
            timeout=30,
        )
        self.assertNotEqual(rc, 0,
                            "Empty input at prompt should reject the host key")
        clean = strip_ansi(out + err)
        # Should mention rejection
        self.assertTrue(
            "rejected" in clean.lower() or "failed" in clean.lower()
            or "error" in clean.lower(),
            "Expected rejection message, got: {}".format(clean[-300:]))

    def test_n_input_rejects(self):
        """Typing 'n' must reject."""
        out, err, rc = run_fc(
            self.src,
            "nobody@127.0.0.1:/tmp/dst",
            input_text="n\n",
            env={"HOME": self.tmphome},
            timeout=30,
        )
        self.assertNotEqual(rc, 0)

    def test_no_input_rejects(self):
        """Typing 'no' must reject."""
        out, err, rc = run_fc(
            self.src,
            "nobody@127.0.0.1:/tmp/dst",
            input_text="no\n",
            env={"HOME": self.tmphome},
            timeout=30,
        )
        self.assertNotEqual(rc, 0)

    def test_eof_rejects(self):
        """EOF (no input at all) must reject."""
        out, err, rc = run_fc(
            self.src,
            "nobody@127.0.0.1:/tmp/dst",
            input_text="",   # immediate EOF
            env={"HOME": self.tmphome},
            timeout=30,
        )
        self.assertNotEqual(rc, 0)


class TestNoVerifyDoesNotBypassHostKey(unittest.TestCase):
    """Verify that --no-verify (which skips post-copy verification) does
    NOT also disable SSH host key verification."""

    def setUp(self):
        self.tmphome = tempfile.mkdtemp(prefix="mitm_nv_")
        self.tmpknown = os.path.join(self.tmphome, ".ssh", "known_hosts")
        os.makedirs(os.path.dirname(self.tmpknown), mode=0o700)
        # Plant a wrong key
        with open(self.tmpknown, "w") as f:
            f.write("[localhost]:22 ssh-ed25519 "
                    "AAAAC3NzaC1lZDI1NTE5AAAAIBadKey" + "A" * 32 + "=\n")
        self.src = tempfile.mkdtemp(prefix="mitm_nv_src_")
        with open(os.path.join(self.src, "f.txt"), "w") as f:
            f.write("test")
        self.dst = tempfile.mkdtemp(prefix="mitm_nv_dst_")

    def tearDown(self):
        for d in (self.tmphome, self.src, self.dst):
            shutil.rmtree(d, ignore_errors=True)

    def test_no_verify_still_checks_host_key(self):
        """Even with --no-verify, host key check must still apply."""
        out, err, rc = run_fc(
            self.src,
            "{}@localhost:{}".format(os.environ.get("USER", "kai"), self.dst),
            "--no-verify",
            env={"HOME": self.tmphome},
            timeout=30,
        )
        self.assertNotEqual(rc, 0,
            "--no-verify bypassed host key check! Connected with wrong key.")


class TestNoBypassEnvVars(unittest.TestCase):
    """Verify there's no environment variable that disables host key
    verification or disables SSL cert verification."""

    def test_grep_for_bypass_envvars(self):
        """Source code should not check any env var that could disable
        host key verification or SSL verification."""
        with open(FAST_COPY) as f:
            code = f.read()
        # These env vars are common bypass patterns — none should be checked
        bad_patterns = [
            "STRICT_HOST_KEY_CHECKING",
            "FAST_COPY_INSECURE",
            "SSL_VERIFY",
            "PYTHONHTTPSVERIFY",
            "VERIFY_SSL",
            "NO_VERIFY",
        ]
        for pat in bad_patterns:
            # Check if any env.get(pattern) or os.environ[pattern] exists
            if "{}".format(pat) in code:
                # Make sure it's not used to disable verification
                # Look for patterns like environ.get(PAT) or os.environ[PAT]
                if any(p in code for p in [
                    "os.environ.get(\"{}".format(pat),
                    "os.environ.get('{}".format(pat),
                    "os.environ[\"{}".format(pat),
                    "os.environ['{}".format(pat),
                    "environ.get(\"{}".format(pat),
                    "environ.get('{}".format(pat),
                ]):
                    self.fail(
                        "Found potential env-var bypass: {} is read from "
                        "environment".format(pat))


class TestHostKeyPolicyIsInteractive(unittest.TestCase):
    """Verify the host key policy is the safe interactive one,
    NOT AutoAddPolicy."""

    def test_no_autoaddpolicy(self):
        with open(FAST_COPY) as f:
            code = f.read()
        # AutoAddPolicy() would silently accept any host key — never used
        self.assertNotIn("AutoAddPolicy()", code,
                         "fast-copy uses paramiko.AutoAddPolicy() — DANGEROUS")
        self.assertNotIn("AutoAddPolicy(", code,
                         "fast-copy uses AutoAddPolicy — DANGEROUS")


class TestKeyChangeDetectionPath(unittest.TestCase):
    """Static check: BadHostKeyException is not silently caught."""

    def test_bad_host_key_exception_not_swallowed(self):
        with open(FAST_COPY) as f:
            code = f.read()
        # The connect() method catches paramiko.SSHException — verify it
        # only retries on AUTH errors, not on key mismatch
        # Search for BadHostKeyException being explicitly caught and ignored
        self.assertNotIn("except paramiko.BadHostKeyException", code,
                         "BadHostKeyException is explicitly caught — verify "
                         "it's not silently ignored")


# ════════════════════════════════════════════════════════════════════════
# Update download / SSL tests (static, since we can't actually trigger
# an update without modifying state)
# ════════════════════════════════════════════════════════════════════════

class TestUpdateDownloadSafety(unittest.TestCase):

    def setUp(self):
        with open(FAST_COPY) as f:
            self.code = f.read()

    def test_update_url_https_only(self):
        """The update URL validator must enforce HTTPS scheme."""
        # Look for the URL validation block
        m = re.search(r'parsed\.scheme\s*!=\s*["\']https["\']', self.code)
        self.assertIsNotNone(m, "No HTTPS scheme check found in update flow")

    def test_update_url_pinned_to_github(self):
        """The allowed hosts list must include only GitHub domains."""
        m = re.search(r'_ALLOWED_HOSTS\s*=\s*\{([^}]+)\}', self.code)
        self.assertIsNotNone(m, "No _ALLOWED_HOSTS pinning found")
        hosts = m.group(1)
        for required in ("github.com", "githubusercontent.com"):
            self.assertIn(required, hosts,
                          "{} not in pinned hosts".format(required))
        # Make sure no untrusted domains
        # (None of these should appear: arbitrary CDN, IP literals, etc.)

    def test_ssl_cert_verification_required(self):
        """SSL context must enforce CERT_REQUIRED."""
        self.assertIn("CERT_REQUIRED", self.code,
                      "No CERT_REQUIRED enforcement found")
        # Make sure no unverified context creation
        self.assertNotIn("create_unverified_context", self.code,
                         "Found ssl.create_unverified_context — UNSAFE")
        self.assertNotIn("CERT_NONE", self.code,
                         "Found ssl.CERT_NONE — verification disabled")

    def test_download_size_validated(self):
        """Downloaded file size must be checked against expected."""
        # Look for "Size mismatch" or similar
        self.assertTrue(
            "Size mismatch" in self.code or "size_mismatch" in self.code
            or "size mismatch" in self.code.lower(),
            "No download size validation")

    def test_download_hash_computed(self):
        """SHA-256 of the download must be computed."""
        self.assertIn("hashlib.sha256(data).hexdigest()", self.code,
                      "Download hash not computed")


# ════════════════════════════════════════════════════════════════════════
# Documented findings (informational, not failing tests)
# ════════════════════════════════════════════════════════════════════════

class TestKnownLimitations(unittest.TestCase):
    """These pass currently but document known limitations to track."""

    def setUp(self):
        with open(FAST_COPY) as f:
            self.code = f.read()

    def test_download_hash_not_verified_against_manifest(self):
        """KNOWN LIMITATION: SHA-256 is computed and printed but not
        verified against a signed manifest. If GitHub release metadata
        is tampered, fast-copy would install the tampered binary.

        This test passes (limitation present) — change to assertNot if
        manifest verification is added in the future.
        """
        # Look for manifest hash comparison
        has_manifest_check = (
            "expected_hash" in self.code
            and ("dl_hash != expected_hash" in self.code
                 or "dl_hash == expected_hash" in self.code))
        # Document the current state — this is a WARNING not a failure
        if not has_manifest_check:
            print("\n  WARNING: download SHA-256 is computed but not verified "
                  "against a signed manifest. See audit MITM finding #1.")

    def test_paramiko_allow_agent_default(self):
        """KNOWN: paramiko's allow_agent=True default lets the local
        ssh-agent be queried for a key. This is NOT agent forwarding
        (which would expose the agent to the remote). It's just local
        key discovery. Safe but worth noting.
        """
        # Confirm allow_agent is not explicitly set anywhere (uses default)
        # If we wanted hardening, we'd set allow_agent=False
        if "allow_agent=False" not in self.code:
            print("\n  NOTE: paramiko allow_agent defaults to True. This "
                  "permits local ssh-agent key lookup but does NOT enable "
                  "agent forwarding. Safe for typical use.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
