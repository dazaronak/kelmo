"""Every environment variable Kelmo reads, and where its value comes from once the daemon runs.

A core is started by kelmod with a stripped environment, so a value a core needs has exactly one home:
  hook-frame  `kelmod hook` reads it in the hook's own process and sends it in the frame
  daemon      `daemon.toml`, or the enrollment or profile the state already holds
  core-spec   `kelmo core-spec` passes it to the core explicitly (environment or descriptor plan)
  cli         read only by a CLI, installer or offline process, never by a core
  removed     the read goes away in the phase named

tests/contract/test_environment.py fails when code under src/kelmo reads a variable that is not listed here.
"""

# name -> (home, phase that moves it, why)
VARIABLES = {
    'KELMO_INTERNAL_JOB': ('hook-frame', 8, 'marks a session started by a Kelmo job; its transcript is not '
                                            'captured; only the hook process can see it, so it travels as '
                                            '`internal_job`'),
    'KELMO_MACHINE_TAG': ('daemon', 3, 'the machine tag comes from the enrollment or the profile, never from '
                                       "the daemon's environment"),
    'KELMO_EMBED_FDS': ('removed', 6, 'the broker channel is handed to the embed core as descriptors by the '
                                      'supervisor; the configured fds_env name goes with it'),
    'KELMO_VAULT': ('cli', None, 'selects the vault of a CLI invocation'),
    'KELMO_TIMINGS': ('cli', None, 'turns on timing output of a CLI invocation'),
    'KELMO_INIT_PARENT': ('cli', None, 'staging directory of `kelmo init`'),
    'GIT_AUTHOR_NAME': ('core-spec', 3, 'Git identity of wiki publications, passed to the state core'),
    'GIT_AUTHOR_EMAIL': ('core-spec', 3, 'Git identity of wiki publications, passed to the state core'),
    'SSH_ORIGINAL_COMMAND': ('cli', None, 'read by the ssh gate; `kelmod attach` reads it in phase 7'),
    'VAULT_ROOT': ('cli', None, 'default vault of the standalone wiki policy script'),
    'XDG_RUNTIME_DIR': ('cli', None, 'base of the daemon socket directory on Linux, as kelmod derives it'),
    'TMPDIR': ('cli', None, 'base of the daemon socket directory elsewhere, as kelmod derives it'),
}

# Reads whose variable name is computed. Each resolves to listed names.
DYNAMIC_READS = {
    'doctor.py': 'the configured embedding fds_env (KELMO_EMBED_FDS by default)',
    'server.py': 'the configured embedding fds_env, forwarded to the archive process',
    'store/config.py': 'the configured embedding fds_env',
    'service.py': 'GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL while initializing a vault',
    'core/spec.py': 'the variables of PASSED_ENVIRONMENT, handed to the state core',
}
