import azure.functions as func
import azure.durable_functions as df
import logging
import requests
import os
import json
import time
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import AzureFunctionStorageQueue, AzureFunctionTool
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.storage.queue import QueueClient, BinaryBase64EncodePolicy, BinaryBase64DecodePolicy
from openai import AzureOpenAI
from datetime import datetime, timedelta

_aoai_client = None

def get_aoai_client() -> AzureOpenAI:
    """Return a singleton AzureOpenAI client authenticated via AAD."""
    global _aoai_client
    if _aoai_client is None:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        _aoai_client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_ad_token_provider=token_provider,
            api_version="2024-10-21",
        )
    return _aoai_client

# Initialize the Durable Functions app with anonymous HTTP authentication level
app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Name of the queues to get and send the function call messages
input_queue_name = "input"
output_queue_name = "output"

def initialize_client():
    """
    Initialize the agent client and the tools Azure Functions that the agent can use.
    """
    # Create an agents client pointed at the AI Foundry project endpoint
    agents_client = AgentsClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )

    # Storage queue service endpoint used to bridge Agent <-> Azure Function
    storage_connection_string = os.environ.get("STORAGE_CONNECTION__queueServiceUri")

    azure_function_tool = AzureFunctionTool(
        name="GitHubIssuesSummaries",
        description="Provide a summary of the GitHub issues for the organization within a specified time period.",
        parameters={
            "type": "object",
            "properties": {
                "organization": {
                    "type": "string",
                    "description": "The organization to find GitHub issues for.",
                },
                "time": {
                    "type": "string",
                    "description": "The specific time period for which the user is querying GitHub issues.",
                },
            },
            "required": ["time"],
        },
        input_queue=AzureFunctionStorageQueue(
            queue_name=input_queue_name,
            storage_service_endpoint=storage_connection_string,
        ),
        output_queue=AzureFunctionStorageQueue(
            queue_name=output_queue_name,
            storage_service_endpoint=storage_connection_string,
        ),
    )

    agent = agents_client.create_agent(
        model=os.environ["AGENT_MODEL_DEPLOYMENT_NAME"],
        name="azure-function-agent-summarize-github-issues",
        instructions="You are a helpful support agent. Answer the user's questions to the best of your ability.",
        tools=azure_function_tool.definitions,
    )
    logging.info(f"Created agent, agent ID: {agent.id}")

    # Create a thread for communication with the agent
    thread = agents_client.threads.create()
    logging.info(f"Created thread, thread ID: {thread.id}")

    return agents_client, thread, agent

@app.route(route="prompt", auth_level=func.AuthLevel.ANONYMOUS)
def prompt(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger function to handle prompts and interact with the agent.
    """
    logging.info('Python HTTP trigger function processed a request.')
    
    # Get the origin from the request
    origin = req.headers.get('Origin', '')
    
    # List of allowed origins - both local and production
    allowed_origins = [
        "http://localhost:3000",
        "https://wonderful-wave-07c299e1e.6.azurestaticapps.net",
        "https://stapp-web-5som3lu6awirw.azurestaticapps.net"
    ]
    
    # Choose the correct origin for CORS response or use * for development
    cors_origin = origin if origin in allowed_origins else "*"
    
    # Handle OPTIONS request for CORS preflight
    if req.method == "OPTIONS":
        return func.HttpResponse(
            status_code=204,
            headers={
                'Access-Control-Allow-Origin': cors_origin,
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization',
                'Access-Control-Allow-Credentials': 'true',
                'Access-Control-Max-Age': '86400'
            }
        )

    # Get the prompt from the request body
    req_body = req.get_json()
    prompt = req_body.get('Prompt')

    # Initialize the agent client
    agents_client, thread, agent = initialize_client()

    last_msg = None
    try:
        # Send the prompt to the agent
        message = agents_client.messages.create(
            thread_id=thread.id,
            role="user",
            content=prompt,
        )
        logging.info(f"Created message, message ID: {message.id}")

        # Run the agent and monitor its status
        run = agents_client.runs.create(thread_id=thread.id, agent_id=agent.id)

        while run.status in ["queued", "in_progress", "requires_action"]:
            time.sleep(1)
            run = agents_client.runs.get(thread_id=thread.id, run_id=run.id)

        logging.info(f"Run finished with status: {run.status}")

        if run.status == "failed":
            logging.error(f"Run failed: {run.last_error}")

        # Get messages from the assistant thread and retrieve the last assistant message
        messages = agents_client.messages.list(thread_id=thread.id)
        for data_point in messages:
            if data_point['role'] == "assistant":
                last_msg = data_point['content'][-1]
                logging.info(f"Last Message: {last_msg.text.value}")
                break
    finally:
        # Delete the agent once done
        try:
            agents_client.delete_agent(agent.id)
        except Exception as e:
            logging.warning(f"Failed to delete agent {agent.id}: {e}")
    
    # Get the origin from the request for response
    origin = req.headers.get('Origin', '')
    
    # List of allowed origins - both local and production
    allowed_origins = [
        "http://localhost:3000",
        "https://wonderful-wave-07c299e1e.6.azurestaticapps.net",
        "https://stapp-web-5som3lu6awirw.azurestaticapps.net"
    ]
    
    # Choose the correct origin for CORS response
    cors_origin = origin if origin in allowed_origins else "*"
    
    # Prepare response with proper CORS headers
    response_message = {"message": last_msg.text.value if last_msg else "No response generated"}
    
    return func.HttpResponse(
        json.dumps(response_message),
        mimetype="application/json",
        headers={
            'Access-Control-Allow-Origin': cors_origin,
            'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Allow-Credentials': 'true'
        }
    )

@app.function_name(name="GitHubIssuesSummaries")
@app.durable_client_input(client_name="client")
@app.queue_trigger(arg_name="msg", queue_name="input", connection="STORAGE_CONNECTION")  
async def process_queue_message(msg: func.QueueMessage, client) -> None:
    """
    Function to start orchestration when a message is received in the queue.
    """
    logging.info('Python queue trigger function processed a queue item')
    
    messagepayload = json.loads(msg.get_body().decode('utf-8'))

    instance_id = await client.start_new("SummarizeGitHubIssues", None, messagepayload)

    logging.info('Started orchestration with ID = {instance_id}')
    
@app.function_name(name="SummarizeGitHubIssues")
@app.orchestration_trigger(context_name="context")
def summarize_github_issues(context: df.DurableOrchestrationContext):
    """
    Orchestrator function to summarize GitHub issues.
    """
    first_retry_interval_in_milliseconds = 5000
    max_number_of_attempts = 3
    retry_options = df.RetryOptions(first_retry_interval_in_milliseconds, max_number_of_attempts)
    
    messagepayload = context.get_input()
    correlation_id = messagepayload['CorrelationId']
    organization = messagepayload.get('organization')
    repo = messagepayload.get('repo')
    
    # Initialize the time dictionary with actual values
    time = {
        "current_date_time": datetime.utcnow().isoformat() + 'Z',
        "prompt_time": messagepayload['time']
    }

    # Query repositories for the organization using an activity function
    if organization:
        repos = yield context.call_activity_with_retry("QueryRepos", retry_options, organization)
        repo_names = repos["repositories"]
    else:
        prompt = f"Return *only* the GitHub organization name for the repository: {repo}"
        organization = yield context.call_activity_with_retry("AskAOAI", retry_options, prompt)
        repo_names = [repo]

    # Correctly format the prompt string using f-string
    prompt = f"Assume the current time is {time['current_date_time']}. Convert the following time to ISO format and return *only* the converted time in the format 'YYYY-MM-DDTHH:MM:SSZ': {time['prompt_time']}"

    # Convert time to ISO format using an activity function
    converted_time = yield context.call_activity_with_retry("AskAOAI", retry_options, prompt)
    
    # Fan-out: Create a list of tasks to get issues for each repository
    tasks = []
    
    for repo_name in repo_names:
        task = context.call_activity_with_retry("QueryNewIssues", retry_options, {'repoName': repo_name, 'orgName': organization, 'time': converted_time})
        tasks.append(task)

    # Fan-in: Wait for all tasks to complete and aggregate the results
    all_issues = yield context.task_all(tasks)

    # Ensure all_issues is initialized before filtering
    if all_issues is None:
        all_issues = []

    # Filter out empty arrays of objects
    allIssues = filter_empty_issues(all_issues)

    # Call the activity function to generate a summary using Azure OpenAI
    summary = yield context.call_activity_with_retry("SummarizeIssues", retry_options, allIssues)

    logging.info(f"summary: {summary}")

    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': summary,
        'CorrelationId': correlation_id
    }

    # Queue to send message to
    queue_client = QueueClient(
        os.environ["STORAGE_CONNECTION__queueServiceUri"],
        queue_name="output",
        credential=DefaultAzureCredential(),
        message_encode_policy=BinaryBase64EncodePolicy(),
        message_decode_policy=BinaryBase64DecodePolicy()
    )

    queue_client.send_message(json.dumps(result_message).encode('utf-8'))

@app.function_name(name="QueryRepos")
@app.activity_trigger(input_name='organization')
def get_repos(organization):
    """
    Activity function to get repositories.
    """
    access_token = os.environ.get("GITHUB_ACCESS_TOKEN")

    url = f"https://api.github.com/orgs/{organization}/repos"
    headers = {
        "Authorization": f"token {access_token}"
    }

    repo_names = []
    page = 1

    while True:
        response = requests.get(url, headers=headers, params={'page': page, 'per_page': 100})
        
        if response.status_code == 200:
            repos = response.json()
            if not repos:
                break
            for repo in repos:
                repo_names.append(repo['name'])
            page += 1
        else:
            print(f"Failed to retrieve repositories: {response.status_code}")
            break

    result = {
        "repositories": repo_names
    }

    return result

@app.function_name(name="AskAOAI")
@app.activity_trigger(input_name='prompt')
def ask_llm(prompt: str):
    """
    Activity function that asks Azure OpenAI a question via chat completions.
    """
    logging.info("in AskAOAI activity")
    client = get_aoai_client()
    completion = client.chat.completions.create(
        model=os.environ["CHAT_MODEL_DEPLOYMENT_NAME"],
        messages=[{"role": "user", "content": prompt}],
    )
    content = completion.choices[0].message.content
    logging.info(content)
    return content
   
@app.function_name(name="QueryNewIssues")
@app.activity_trigger(input_name='queryDetails')
def get_issues(queryDetails):
    """
    Activity function to get issues for a repository.
    """
    organization = queryDetails['orgName']
    repoName = queryDetails['repoName']
    since = queryDetails['time']

    access_token = os.environ.get("GITHUB_ACCESS_TOKEN")

    url = f"https://api.github.com/repos/{organization}/{repoName}/issues"

    headers = {
        "Authorization": f"token {access_token}"
    }
    
    params = {
        "since": since,
    }

    response = requests.get(url, headers=headers, params=params)
    issues = []

    if response.status_code == 200:
        issues_data = response.json()
        if issues_data:
            repo_issues = []
            for issue in issues_data:
                filtered_issue = {
                    "state": issue.get("state"),
                    "user": issue.get("user", {}).get("login"),
                    "title": issue.get("title"),
                    "body": issue.get("body")
                }
                repo_issues.append(filtered_issue)
            issues.append({repoName: repo_issues})
    else:
        print(f"Failed to retrieve issues for {repoName}: {response.status_code}")

    return issues

@app.function_name(name="SummarizeIssues")
@app.activity_trigger(input_name='allIssues')
def summarize_issues(allIssues):
    """
    Activity function to generate a summary using Azure OpenAI chat completions.
    """
    logging.info("in SummarizeIssues activity")
    client = get_aoai_client()
    completion = client.chat.completions.create(
        model=os.environ["CHAT_MODEL_DEPLOYMENT_NAME"],
        messages=[{"role": "user", "content": f"Generate a summary of the following GitHub issues and determine: {allIssues}"}],
        max_tokens=1000,
    )
    return completion.choices[0].message.content

def filter_empty_issues(allIssues):
    """
    Function to filter out empty arrays of objects.
    """
    return [issue for issue in allIssues if issue]