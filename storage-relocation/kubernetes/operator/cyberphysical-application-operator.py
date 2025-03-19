import kopf, requests, json, random, datetime, os
from kubernetes import client
from kubernetes.utils import create_from_dict
from k8s_utils import delete_from_dict
from graph_utils import generate_chart

@kopf.on.create("cyberphysicalapplications")
def create_fn(spec, name, logger, **kwargs):
    k8s_client = client.ApiClient()

    deployments = spec.get("deployments")
    preferred_affinity = spec.get("requirements").get("preferredAffinity")

    deployment_affinity = None
    deployment_configs = None
    deployment_namespace = None
    deployment_app_name = None
    deployment_prometheus_url = None

    if preferred_affinity == "" or preferred_affinity is None:
        deployment_configs = deployments[0].get("configs")
        deployment_affinity = deployments[0].get("affinity")

    else:
        for deployment in deployments:
            deployment_affinity = deployment.get("affinity")
            if deployment_affinity == preferred_affinity:
                deployment_configs = deployment.get("configs")
                break

    for config in deployment_configs:
        if config.get("kind") == "Deployment":
            config["spec"]["template"]["spec"].update(
                {"nodeSelector": {"zone": f"{deployment_affinity}"}}
            )
            deployment_namespace = config.get("metadata").get("namespace")
            deployment_app_name = config.get("metadata").get("labels").get("app")
            deployment_prometheus_url = (
                config.get("spec")
                .get("template")
                .get("metadata")
                .get("annotations")
                .get("prometheusUrl")
            )

        kopf.label(config, {"related-to": f"{name}"})
        kopf.adopt(config)
        try:
            create_from_dict(k8s_client, config)
        except Exception as e:
            logger.exception("Exception in object creation.")

    return {
        "child-deployment-namespace": deployment_namespace,
        "child-deployment-app-name": deployment_app_name,
        "child-deployment-prometheus-url": deployment_prometheus_url,
        "child-deployment-affinity": deployment_affinity,
    }


def get_prometheus_odte(prometheus_url, twin_of, logger):
    query_url = f'{prometheus_url}/api/v1/query?query=odte[pt="{twin_of}"]'
    query_url = query_url.replace("[", "{").replace("]", "}")
    logger.debug(query_url)

    odte = None
    try:
        resp = requests.get(query_url)
    except:
        logger.info("Prometheus not available.")
        raise Exception

    try:
        odte = float(json.loads(resp.text)["data"]["result"][0]["value"][1])
    except:
        logger.info("ODTE not available.")
        raise Exception

    return odte


def choose_next_deployment(deployments, current_deployment_affinity):
    next_depl_index = random.randint(0, len(deployments) - 1)
    next_depl_affinity = deployments[next_depl_index].get("affinity")

    while next_depl_affinity == current_deployment_affinity:
        next_depl_index = random.randint(0, len(deployments) - 1)
        next_depl_affinity = deployments[next_depl_index].get("affinity")

    return deployments[next_depl_index]


def ensure_pod_termination(k8s_core_v1, app_name, namespace, logger):
    terminated = False
    label_selector = f"app={app_name}"
    while not terminated:
        try:
            resp = k8s_core_v1.list_namespaced_pod(
                namespace, label_selector=label_selector
            )
            if len(resp.items) == 0:
                terminated = True
        except:
            logger.info("Cannot list pods.")


def ensure_pods_ready(k8s_core_v1, app_name, namespace, logger):
    label_selector = f"app={app_name}"
    try:
        resp = k8s_core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
    except:
        logger.error("Cannot list pods.")
        return

    pods = resp.items

    for pod in pods:
        pod_ready = False
        pod_name = pod.metadata.name

        while not pod_ready:
            try:
                pod_status = k8s_core_v1.read_namespaced_pod_status(pod_name, namespace)
            except:
                logger.error("Cannot read pod status.")

            conditions = pod_status.status.conditions
            for condition in conditions:
                if condition.type == "Ready" and condition.status == "True":
                    pod_ready = True


@kopf.on.update("cyberphysicalapplications")
def update_fn(name, spec, namespace, **kwargs):

    k8s_client_custom_object = client.CustomObjectsApi()
    migrate_field = spec.get("migrate", None)
    if migrate_field:
        cpa_patch = {"spec": {"migrate": migrate_field}}
        group = "test.dev"
        version = "v1"
        plural = "cyberphysicalapplications"
        obj = k8s_client_custom_object.patch_namespaced_custom_object(
            group, version, namespace, plural, name, body=cpa_patch
        )


@kopf.on.field("cyberphysicalapplications", field="spec.migrate")
def migrate_fn(spec, meta, status, name, old, new, logger, namespace, **_):

    # guard condition for creation
    if old is None:
        return

    k8s_client = client.ApiClient()
    k8s_core_v1 = client.CoreV1Api()
    k8s_custom_object = client.CustomObjectsApi()

    # trigger a migration
    if old == False and new == True:
        timestamps = {}

        deployments = spec.get("deployments")
        current_deployment_affinity = status.get("create_fn").get(
            "child-deployment-affinity"
        )
        current_deployment_namespace = status.get("create_fn").get(
            "child-deployment-namespace"
        )
        next_deployment = choose_next_deployment(
            deployments, current_deployment_affinity
        )
        next_deployment_configs = next_deployment.get("configs")
        next_deployment_affinity = next_deployment.get("affinity")

        timestamps["Choosing next deployment"] = datetime.datetime.now()

        # find rsync source
        label_selector = f"related-to={name}"
        resp = k8s_core_v1.list_namespaced_service(
            current_deployment_namespace, label_selector=label_selector
        )
        current_deployment_service_name = resp.items[0].metadata.name

        # start new instance
        annotations_patch = {"metadata": {"annotations": dict(meta.annotations)}}
        for config in next_deployment_configs:
            if config.get("kind") == "Deployment":
                config["spec"]["template"]["spec"].update(
                    {"nodeSelector": {"zone": f"{next_deployment_affinity}"}}
                )
                next_deployment_namespace = config.get("metadata").get("namespace")
                next_deployment_app_name = (
                    config.get("metadata").get("labels").get("app")
                )
                annotations_patch["metadata"]["annotations"][
                    "child-deployment-namespace"
                ] = next_deployment_namespace
                next_deployment_app_name = (
                    config.get("metadata").get("labels").get("app")
                )
                annotations_patch["metadata"]["annotations"][
                    "child-deployment-app-name"
                ] = next_deployment_app_name
                # status["create_fn"]["child-deployment-prometheus-url"] = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
                annotations_patch["metadata"]["annotations"][
                    "child-deployment-affinity"
                ] = next_deployment_affinity

                # set rsync env vars to trigger rsync in init container
                init_container = (
                    config.get("spec")
                    .get("template")
                    .get("spec")
                    .get("initContainers")[0]
                )
                init_container["env"] = [
                    {"name": "RSYNC_SOURCE", "value": current_deployment_service_name},
                    {"name": "RSYNC_SOURCE_PATH", "value": "dt_data/dump.json"},
                    {"name": "RSYNC_DEST_PATH", "value": "/var/tmp/dt_data"},
                ]

                config["spec"]["template"]["spec"]["initContainers"][0].update(
                    init_container
                )

            kopf.adopt(config)
            kopf.label(config, {"related-to": f"{name}"})
            try:
                create_from_dict(k8s_client, config)
                pass
            except:
                logger.exception("Exception creating new object.")

        timestamps["Creating new instance"] = datetime.datetime.now()

        # wait rsync, check if init pods are terminated
        init_pod_name = "rsync-init"
        label_selector = f"app={next_deployment_app_name}"
        terminated = False
        resp = k8s_core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
        while len(resp.items) == 0:
            resp = k8s_core_v1.list_namespaced_pod(
                namespace, label_selector=label_selector
            )

        while not terminated:
            resp = k8s_core_v1.list_namespaced_pod(
                namespace, label_selector=label_selector
            )
            init_container_statuses = resp.items[0].status.init_container_statuses

            if not init_container_statuses is None:
                for init_container_status in init_container_statuses:
                    if init_container_status.name == init_pod_name:
                        if not init_container_statuses[0].state.terminated is None:
                            terminated = True

        timestamps["Performing rsync"] = datetime.datetime.now()

        # delete old instance
        for depl in deployments:
            if depl.get("affinity") == current_deployment_affinity:
                for config in depl.get("configs"):
                    delete_from_dict(k8s_client, config)

        timestamps["Deleting old instance"] = datetime.datetime.now()

        group = "test.dev"
        version = "v1"
        plural = "cyberphysicalapplications"
        resp = k8s_custom_object.patch_namespaced_custom_object(
            group, version, namespace, plural, name, body=annotations_patch
        )

        generate_chart(timestamps)
        return
