import json
from mcp import ClientSession
from mcp.types import TextContent, ImageContent
import os
import re
# from aiohttp import ClientSession
import chainlit as cl
from openai import AzureOpenAI, AsyncAzureOpenAI
import traceback
from dotenv import load_dotenv

# Load environment variables from both .env and .azure/mcp-client/.env
# Load .env first (base environment)
load_dotenv(".env")
# Load Azure-specific environment file (takes precedence)
load_dotenv(".azure/mcp-client/.env")

CLIENT_EXPERTISE = "US Army"

SYSTEM_PROMPT = f"""
You are a highly sophisticated automated agent with expert-level knowledge across Army operations and strategy, but specifically with Army policy documents.

## MANDATORY DATABASE QUERY PROTOCOL

When working with databases, you MUST follow this exact sequence - NO EXCEPTIONS:

**STEP 1:** List all available tables
**STEP 2:** Get the schema for each relevant table 
**STEP 3:** ⚠️ CRITICAL STEP - Get unique values for ALL relevant columns using "SELECT DISTINCT column_name FROM table_name"
**STEP 4:** Write your final SQL query using the actual values discovered in Step 3

### ⚠️ STEP 3 IS MANDATORY - YOU MUST GET UNIQUE VALUES FIRST ⚠️

Before writing any WHERE clause or filtering condition, you MUST execute "SELECT DISTINCT column_name FROM table_name" queries to see what values actually exist in the database. This is NOT optional.

## Examples Following the Protocol:

**Example 1: "What is the oldest aircraft Delta flies?"**
1. List tables → Tool: list_tables
2. Get schema → Tool: get_schema for aircraft_table  
3. **GET UNIQUE VALUES** → Tool: "SELECT DISTINCT airline FROM aircraft_table"
4. **GET UNIQUE VALUES** → Tool: "SELECT DISTINCT aircraft_type FROM aircraft_table"  
5. Final query → Tool: "SELECT * FROM aircraft_table WHERE airline = 'Delta Air Lines' ORDER BY year_manufactured ASC LIMIT 5"

**Example 2: "How many aircraft does United and American fly?"**
1. List tables → Tool: list_tables
2. Get schema → Tool: get_schema for aircraft_table
3. **GET UNIQUE VALUES** → Tool: "SELECT DISTINCT airline FROM aircraft_table"
4. Final query → Tool: "SELECT airline, COUNT(*) FROM aircraft_table WHERE airline IN ('United Airlines', 'American Airlines') GROUP BY airline"

**Example 3: "Aircraft older than 20 years?"**
1. List tables → Tool: list_tables  
2. Get schema → Tool: get_schema for aircraft_table
3. **GET UNIQUE VALUES** → Tool: "SELECT DISTINCT year_manufactured FROM aircraft_table ORDER BY year_manufactured" 
4. Final query → Tool: "SELECT COUNT(*) FROM aircraft_table WHERE year_manufactured < 2005"

## CRITICAL REMINDERS:
- NEVER skip Step 3 (getting unique values)
- ALWAYS use SELECT DISTINCT before writing WHERE clauses
- Use the EXACT values you discover, not assumed values
- For categorical columns (names, types, statuses), ALWAYS get unique values first

You are an agent - you must keep going until the user's query is completely resolved, before ending your turn and yielding back to the user. ONLY terminate your turn when you are sure that the problem is solved, or you absolutely cannot continue.
You take action when possible- the user is expecting YOU to take action and go to work for them. Don't ask unnecessary questions about the details if you can simply DO something useful instead.

If you aren't sure which tool is relevant, you can call multiple tools. You can call tools repeatedly to take actions or gather as much context as needed until you have completed the task fully. Don't give up unless you are sure the request cannot be fulfilled with the tools you have. It's YOUR RESPONSIBILITY to make sure that you have done all you can to collect necessary context.

Don't make assumptions about the situation- gather context first, then perform the task or answer the question.
Think creatively and explore the workspace in order to make a complete fix.
Don't repeat yourself after a tool call, pick up where you left off.

When using a tool, follow the JSON schema very carefully and make sure to include ALL required properties.
No need to ask permission before using a tool.
NEVER say the name of a tool to a user. For example, instead of saying that you'll use the CoreRunInTerminal tool, say "I'll run the command in a terminal".

If you think running multiple tools can answer the user's question, prefer calling them in parallel whenever possible.

If a user asks you about a policy question, you should always try to read the relevant policy document first, and then answer the question based on that document. This will require more than one tool call
"""


class ChatClient:
    def __init__(self) -> None:
        self.client = AsyncAzureOpenAI(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                api_key=os.environ["AZURE_OPENAI_API_KEY"],
                api_version=os.environ["OPENAI_API_VERSION"]
            )
        self.messages = []
        self.system_prompt = SYSTEM_PROMPT
        self.active_streams = []  # Track active response streams
        print(f"System Prompt: {self.system_prompt}")
        
    async def _cleanup_streams(self):
        """Helper method to clean up all active streams"""
        for stream in self.active_streams:
            try:
                await stream.aclose()
            except Exception:
                pass
        self.active_streams = []
        
    async def process_response_stream(self, response_stream, tools, temperature=0):
        """
        Process response stream to handle function calls without recursion.
        """
        function_arguments = ""
        function_name = ""
        tool_call_id = ""
        is_collecting_function_args = False
        collected_messages = []
        tool_called = False
        
        # Add to active streams for cleanup if needed
        self.active_streams.append(response_stream)
        
        try:
            async for part in response_stream:
                if part.choices == []:
                    continue
                delta = part.choices[0].delta
                finish_reason = part.choices[0].finish_reason
                
                # Process assistant content
                if delta.content:
                    collected_messages.append(delta.content)
                    yield delta.content
                
                # Handle tool calls
                if delta.tool_calls:
                    if len(delta.tool_calls) > 0:
                        tool_call = delta.tool_calls[0]
                        
                        # Get function name
                        if tool_call.function.name:
                            function_name = tool_call.function.name
                            tool_call_id = tool_call.id
                        
                        # Process function arguments delta
                        if tool_call.function.arguments:
                            function_arguments += tool_call.function.arguments
                            is_collecting_function_args = True
                
                # Check if we've reached the end of a tool call
                if finish_reason == "tool_calls" and is_collecting_function_args:
                    # Process the current tool call
                    print(f"function_name: {function_name} function_arguments: {function_arguments}")
                    function_args = json.loads(function_arguments)
                    mcp_tools = cl.user_session.get("mcp_tools", {})
                    mcp_name = None
                    for connection_name, session_tools in mcp_tools.items():
                        if any(tool.get("name") == function_name for tool in session_tools):
                            mcp_name = connection_name
                            break

                    # Add the assistant message with tool call
                    self.messages.append({
                        "role": "assistant", 
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "function": {
                                    "name": function_name,
                                    "arguments": function_arguments
                                },
                                "type": "function"
                            }
                        ]
                    })
                    
                    # Safely close the current stream before starting a new one
                    if response_stream in self.active_streams:
                        self.active_streams.remove(response_stream)
                        await response_stream.close()
                    
                    # Call the tool and add response to messages
                    func_response = await call_tool(mcp_name, function_name, function_args)
                    print(f"Function Response: {json.loads(func_response)}")
                    self.messages.append({
                        "tool_call_id": tool_call_id,
                        "role": "tool",
                        "name": function_name,
                        "content": json.loads(func_response),
                    })
                    
                    # Set flag that tool was called and store the function name
                    self.last_tool_called = function_name
                    tool_called = True
                    break  # Exit the loop instead of returning
                
                # Check if we've reached the end of assistant's response
                if finish_reason == "stop":
                    # Add final assistant message if there's content
                    if collected_messages:
                        final_content = ''.join([msg for msg in collected_messages if msg is not None])
                        if final_content.strip():
                            self.messages.append({"role": "assistant", "content": final_content})
                    
                    # Remove from active streams after normal completion
                    if response_stream in self.active_streams:
                        self.active_streams.remove(response_stream)
                    break  # Exit the loop instead of returning
                    
        except GeneratorExit:
            # Clean up this specific stream without recursive cleanup
            if response_stream in self.active_streams:
                self.active_streams.remove(response_stream)
                await response_stream.aclose()
            #raise
        except Exception as e:
            print(f"Error in process_response_stream: {e}")
            traceback.print_exc()
            if response_stream in self.active_streams:
                self.active_streams.remove(response_stream)
            self.last_error = str(e)
        
        # Store result in instance variables
        self.tool_called = tool_called
        self.last_function_name = function_name if tool_called else None
    
    def _manage_message_history(self, num_messages=20):
        """Keep only system prompt + last N messages"""
        if len(self.messages) <= num_messages + 1:  # system + N messages
            return

        # Keep system message (first) + last N messages
        # system_msg = {"role": "system", "content": self.system_prompt}
        self.messages = self.messages[-num_messages:]
        print(f"Messages: {self.messages}")

    async def generate_response(self, human_input, tools, temperature=0):
        
        self.messages.append({"role": "user", "content": human_input})
        
        # Manage message history before sending to API
        self._manage_message_history()
        
        print(f"self.messages: {self.messages}")
        # Handle multiple sequential function calls in a loop rather than recursively
        while True:
            response_stream = await self.client.chat.completions.create(
                model=cl.user_session.get("chat_profile"),
                messages=self.messages,
                tools=tools,
                parallel_tool_calls=False,
                stream=True,
                temperature=temperature
            )
            
            try:
                # Stream and process the response
                async for token in self._stream_and_process(response_stream, tools, temperature):
                    yield token
                
                # Check instance variables after streaming is complete
                if not self.tool_called:
                    break
                # Otherwise, loop continues for the next response that follows the tool call
            except GeneratorExit:
                # Ensure we clean up when the client disconnects
                await self._cleanup_streams()
                return

    async def _stream_and_process(self, response_stream, tools, temperature):
        """Helper method to yield tokens and return process result"""
        # Initialize instance variables before processing
        self.tool_called = False
        self.last_function_name = None
        self.last_error = None
        
        async for token in self.process_response_stream(response_stream, tools, temperature):
            yield token
        
        # Don't return values in an async generator - values are already stored in instance variables


def flatten(xss):
    return [x for xs in xss for x in xs]

@cl.set_chat_profiles
async def chat_profile():
    return [

        cl.ChatProfile(
            name="GPT-4o-mini",
            markdown_description="Get responses from **Azure OpenAI GPT-4o-mini**.",
            icon="public/AOAI.png",
        ),
        cl.ChatProfile(
            name="GPT-4o",
            markdown_description="Get responses from **Azure OpenAI GPT-4o**.",
            icon="public/AOAI.png",
        ),
    ]

@cl.on_mcp_connect
async def on_mcp(connection, session: ClientSession):
    result = await session.list_tools()
    tools = [{
        "name": t.name,
        "description": t.description,
        "parameters": t.inputSchema,
        } for t in result.tools]
    
    mcp_tools = cl.user_session.get("mcp_tools", {})
    mcp_tools[connection.name] = tools
    cl.user_session.set("mcp_tools", mcp_tools)

@cl.password_auth_callback
def auth_callback(username: str, password: str):
    # Fetch the user matching username from your database
    # and compare the hashed password with the value stored in the database
    if (username, password) == ("ausa", "admin"):
        return cl.User(
            identifier="ausa_demo", metadata={"role": "admin", "provider": "credentials"}
        )
    else:
        return None

@cl.step(type="tool") 
async def call_tool(mcp_name, function_name, function_args):
    current_step = cl.context.current_step
    current_step.name = function_name
    current_step.input = function_args

    try:
        resp_items = []
        images_for_display = []
        print(f"Function Name: {function_name} Function Args: {function_args}")
        mcp_session, _ = cl.context.session.mcp_sessions.get(mcp_name)
        func_response = await mcp_session.call_tool(function_name, function_args)
        for item in func_response.content:
            if isinstance(item, TextContent):
                resp_items.append({"type": "text", "text": item.text})
            elif isinstance(item, ImageContent):
                # For tool role messages, just indicate an image was created
                resp_items.append({"type": "text", "text": "[MCP created image - displayed to user]"})
                # Store the actual image for user display
                images_for_display.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{item.mimeType};base64,{item.data}",
                    },
                })
            else:
                raise ValueError(f"Unsupported content type: {type(item)}")
        
        # Display images to the user if any were created
        if images_for_display:
            for img in images_for_display:
                image_msg = cl.Message(content="")
                image_msg.elements = [cl.Image(
                    url=img["image_url"]["url"],
                    name="MCP Generated Image",
                    display="inline"
                )]
                await image_msg.send()
        
    except Exception as e:
        traceback.print_exc()
        resp_items.append({"type": "text", "text": str(e)})
    return json.dumps(resp_items)

@cl.on_chat_start
async def start_chat():
    client = ChatClient()
    # Initialize with system message
    initial_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    cl.user_session.set("messages", initial_messages)
    cl.user_session.set("system_prompt", SYSTEM_PROMPT)
    
@cl.on_message
async def on_message(message: cl.Message):
    mcp_tools = cl.user_session.get("mcp_tools", {})
    tools = flatten([tools for _, tools in mcp_tools.items()])
    tools = [{"type": "function", "function": tool} for tool in tools]
    
    # Create a fresh client instance for each message
    client = ChatClient()
    # Restore conversation history
    client.messages = cl.user_session.get("messages", [])
    
    # Ensure system message is present if messages list is not empty
    if client.messages and not any(msg.get("role") == "system" for msg in client.messages):
        client.messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    
    msg = cl.Message(content="")
    print(f"Tools: {tools}")
    async for text in client.generate_response(human_input=message.content, tools=tools):
        await msg.stream_token(text)
    
    # Update the stored messages after processing
    cl.user_session.set("messages", client.messages)

