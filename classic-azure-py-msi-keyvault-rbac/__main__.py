from pulumi_azure import core, storage, mssql, appservice, keyvault, authorization
from pulumi import export, Output, asset
import pulumi_random as random


def createFirewallRules(arg):
    ips = arg.split(",")
    for ip in ips:
        rule = mssql.FirewallRule(
            "FR%s" % ip,
            resource_group_name=resource_group.name,
            start_ip_address=ip,
            end_ip_address=ip,
            server_name=sql_server.name,
        )


resource_group = core.ResourceGroup("resourceGroup")

storage_account = storage.Account(
    "storage",
    resource_group_name=resource_group.name,
    account_replication_type="LRS",
    account_tier="Standard",
)

container = storage.Container(
    "files", storage_account_name=storage_account.name, container_access_type="private"
)

administrator_login_password = random.RandomPassword(
    "password",
    length=16,
    special=True,
).result

sql_server = mssql.Server(
    "sqlserver",
    resource_group_name=resource_group.name,
    administrator_login_password=administrator_login_password,
    administrator_login="manualadmin",
    version="12.0",
)


database = mssql.Database("sqldb", server_id=sql_server.id, sku_name="S0")

connection_string = (
    Output.all(sql_server.name, database.name).apply(
        lambda args: f"Server=tcp:{args[0]}.database.windows.net;Database={args[1]};"
    )
    or "1111"
)

text_blob = storage.Blob(
    "text",
    storage_account_name=storage_account.name,
    storage_container_name=container.name,
    type="Block",
    source=asset.FileAsset("./README.md"),
)

app_service_plan = appservice.ServicePlan(
    "asp",
    resource_group_name=resource_group.name,
    os_type="Linux",
    sku_name="B1",
)

blob = storage.Blob(
    "zip",
    storage_account_name=storage_account.name,
    storage_container_name=container.name,
    type="Block",
    source=asset.FileArchive("./webapp/bin/Debug/net8.0/publish"),
)

client_config = core.get_client_config()
tenant_id = client_config.tenant_id
current_principal = client_config.object_id

vault = keyvault.KeyVault(
    "vault",
    resource_group_name=resource_group.name,
    sku_name="standard",
    tenant_id=tenant_id,
    access_policies=[
        keyvault.KeyVaultAccessPolicyArgs(
            tenant_id=tenant_id,
            object_id=current_principal,
            secret_permissions=["Delete", "Get", "List", "Set"],
        )
    ],
)

blob_sas = storage.get_account_blob_container_sas_output(
    connection_string=storage_account.primary_connection_string,
    start="2020-01-01",
    expiry="2030-01-01",
    container_name=container.name,
    permissions=storage.GetAccountBlobContainerSASPermissionsArgs(
        read=True, write=False, delete=False, list=False, add=False, create=False
    ),
)

signed_blob_url = Output.concat(
    "https://",
    storage_account.name,
    ".blob.core.windows.net/",
    storage_account.name,
    "/",
    blob.name,
    blob_sas.sas,
)

secret = keyvault.Secret("deployment-zip", key_vault_id=vault.id, value=signed_blob_url)

secret_uri = Output.all(vault.vault_uri, secret.name, secret.version).apply(
    lambda args: f"{args[0]}secrets/{args[1]}/{args[2]}"
)

app = appservice.LinuxWebApp(
    "app",
    resource_group_name=resource_group.name,
    service_plan_id=app_service_plan.id,
    identity=appservice.AppServiceIdentityArgs(
        type="SystemAssigned",
    ),
    site_config={},
    app_settings={
        "WEBSITE_RUN_FROM_ZIP": secret_uri.apply(
            lambda args: "@Microsoft.KeyVault(SecretUri=" + args + ")"
        ),
        "StorageBlobUrl": text_blob.url,
    },
    connection_strings=[
        appservice.AppServiceConnectionStringArgs(
            name="db",
            value=connection_string,
            type="SQLAzure",
        )
    ],
)

## Work around a preview issue https://github.com/pulumi/pulumi-azure/issues/192
principal_id = app.identity.apply(
    lambda id: id.principal_id or "11111111-1111-1111-1111-111111111111"
)

policy = keyvault.AccessPolicy(
    "app-policy",
    key_vault_id=vault.id,
    tenant_id=tenant_id,
    object_id=principal_id,
    secret_permissions=["Get"],
)

blob_permission = authorization.Assignment(
    "readblob",
    principal_id=principal_id,
    role_definition_name="Storage Blob Data Reader",
    scope=Output.all(storage_account.id, container.name).apply(
        lambda args: f"{args[0]}/blobServices/default/containers/{args[1]}"
    ),
)

ips = app.outbound_ip_addresses.apply(createFirewallRules)

export("endpoint", app.default_hostname.apply(lambda endpoint: "https://" + endpoint))
