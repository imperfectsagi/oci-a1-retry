"""Retry launching one Oracle Cloud Always Free VM.Standard.A1.Flex (2 OCPU / 12 GB)
until capacity frees up. Stops as soon as an instance with the target name exists.
Never launches more than one A1 instance, never uses a paid shape."""
import json, os, sys, time
import oci

cfg = json.load(open("config.json"))
oci_config = {
    "user": os.environ["OCI_USER"],
    "tenancy": os.environ["OCI_TENANCY"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region": os.environ["OCI_REGION"],
    "key_content": os.environ["OCI_PRIVATE_KEY"],
}
oci.config.validate_config(oci_config)
compute = oci.core.ComputeClient(oci_config)
identity = oci.identity.IdentityClient(oci_config)
network = oci.core.VirtualNetworkClient(oci_config)
comp = cfg.get("compartment_id") or os.environ["OCI_TENANCY"]
SHAPE = "VM.Standard.A1.Flex"
deadline = time.time() + cfg.get("run_minutes", 340) * 60


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), msg, flush=True)


def existing_a1():
    insts = oci.pagination.list_call_get_all_results(compute.list_instances, comp).data
    return [i for i in insts if i.shape == SHAPE and i.lifecycle_state not in ("TERMINATED", "TERMINATING")]


def success(inst):
    log(f"SUCCESS: {inst.display_name} state={inst.lifecycle_state}")
    open("success.flag", "w").write(inst.id)
    sys.exit(0)


subnet_id = cfg.get("subnet_id") or next(
    sn.id for sn in network.list_subnets(comp, display_name=cfg["subnet_name"]).data
    if sn.lifecycle_state == "AVAILABLE")
log("subnet found")
ad = identity.list_availability_domains(comp).data[0].name
images = compute.list_images(
    comp, operating_system="Canonical Ubuntu", operating_system_version=cfg.get("ubuntu_version", "24.04"),
    shape=SHAPE, sort_by="TIMECREATED", sort_order="DESC").data
images = [i for i in images if "aarch64" in i.display_name and "Minimal" not in i.display_name] or images
image_id = images[0].id
log(f"image={images[0].display_name}")

details = oci.core.models.LaunchInstanceDetails(
    compartment_id=comp,
    availability_domain=ad,
    display_name=cfg["instance_name"],
    shape=SHAPE,
    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12),
    source_details=oci.core.models.InstanceSourceViaImageDetails(
        image_id=image_id, boot_volume_size_in_gbs=cfg.get("boot_gb", 50)),
    create_vnic_details=oci.core.models.CreateVnicDetails(
        subnet_id=subnet_id, assign_public_ip=True),
    metadata={"ssh_authorized_keys": cfg["ssh_public_key"]},
    is_pv_encryption_in_transit_enabled=True,
)

attempt = 0
while time.time() < deadline:
    attempt += 1
    try:
        found = existing_a1()
        if found:
            success(found[0])
        inst = compute.launch_instance(details).data
        success(inst)
    except oci.exceptions.ServiceError as e:
        msg = (e.message or "").lower()
        if e.status == 429:
            wait = 180
        elif "capacity" in msg or e.status in (500, 502, 503, 504):
            wait = cfg.get("retry_seconds", 90)
        else:
            wait = 300
        log(f"attempt {attempt}: {e.status} {e.code} {e.message[:160] if e.message else ''} -> retry in {wait}s")
        time.sleep(wait)
    except Exception as e:  # network hiccups
        log(f"attempt {attempt}: {type(e).__name__}: {e} -> retry in 120s")
        time.sleep(120)
log("Run window over without capacity; next scheduled run continues.")
