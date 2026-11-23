- Tenant filter, constraint scan, actor class (config-een-oppervlak N5): `VNX_PROJECT_FILTER`, `VNX_PROVIDER_ENV`,
  `VNX_WORKER_ENV` and `VNX_PROCESS_OWNER` are deleted. The tenant filter always holds, the SDK scan reads
  `WORKER_ENV` and `VNX_INSTRUCTION`, and a process with no TTY and no agent marker is `unknown`.
