import os
import uuid
import logging
import httpx
from discord import Message
from discord.ext import commands
from a2a.client import A2AClient
from a2a.types import SendMessageRequest, MessageSendParams, Message as A2AMessage, TextPart, Role

logger = logging.getLogger(__name__)
SET = set()

MAX_LENGTH = 2000

channel_contexts = {}

def split_by_lines(text, max_len=MAX_LENGTH):
    lines = text.splitlines(keepends=True)
    chunks = []
    current = ""

    for line in lines:
        if len(current) + len(line) > max_len:
            chunks.append(current)
            current = line
        else:
            current += line

    if current:
        chunks.append(current)

    return chunks


async def invoke_a2a_agent(agent_url: str, input: str, channel_id: str):
    async with httpx.AsyncClient(timeout=600.0) as httpx_client:
        a2a_client = A2AClient(httpx_client=httpx_client, url=agent_url)
        
        existing_context_id = channel_contexts.get(channel_id)
        if existing_context_id:
            logger.info(f"Using existing context for channel {channel_id}")
        
        text_part = TextPart(text=input)
        message = A2AMessage(
            messageId=str(uuid.uuid4()),
            role=Role.user,
            parts=[text_part],
            contextId=existing_context_id         )
        
        params = MessageSendParams(message=message)
        
        request = SendMessageRequest(
            id=str(uuid.uuid4()),
            params=params
        )
        
        response = await a2a_client.send_message(request)
        
        logger.info(f"Received response from agent for channel {channel_id}")

        if hasattr(response.root, 'error') and response.root.error:
            return f"Agent returned error: {response.root.error.message}"
        
        if hasattr(response.root, 'result') and response.root.result:
            result = response.root.result
            
            context_id = getattr(result, 'context_id', None)  # Task uses context_id in Python
            
            if context_id:
                logger.info(f"Storing context for channel {channel_id}")
                channel_contexts[channel_id] = context_id
            elif hasattr(result, 'id') and result.id:
                logger.info(f"Using task ID as context for channel {channel_id}")
                channel_contexts[channel_id] = result.id
            else:
                logger.warning(f"No contextId found in result for session continuity")
            
            if hasattr(result, 'artifacts') and result.artifacts:
                extracted_texts = []
                for artifact in result.artifacts:
                    if hasattr(artifact, 'parts') and artifact.parts:
                        for part in artifact.parts:
                            actual_part = part.root
                            if hasattr(actual_part, 'text'):
                                extracted_texts.append(actual_part.text)
                
                return "".join(extracted_texts)
            
            elif hasattr(result, 'parts') and result.parts:
                return "".join(
                    part.root.text
                    for part in result.parts
                    if hasattr(part.root, 'text')
                )
            
            else:
                logger.warning(f"Unexpected result format")
                return str(result)
        else:
            logger.warning("No result found in response")
        
        return "No response received from agent"


def register_handlers(bot: commands.Bot):
    mention_only = os.getenv("DISCORD_MENTION_ONLY", "false").lower() == "true"
    allowed_channels_raw = os.getenv("DISCORD_CHANNEL_ONLY", "")
    allowed_channels = [c.strip() for c in allowed_channels_raw.split(",") if c.strip().isdigit()]

    @bot.event
    async def on_message(message: Message):
        if message.author.bot:
            return
        content = message.content.strip()
        user_id = message.author.id
        channel_id = str(message.channel.id)

        if mention_only and message.guild and message.guild.me not in message.mentions:
            return

        if allowed_channels and channel_id not in allowed_channels:
            return

        logger.info(f"Processing message in channel {channel_id}")

        if content.lower() in ['!reset', '!clear', '!new']:
            if channel_id in channel_contexts:
                del channel_contexts[channel_id]
                await message.reply("🔄 Session context cleared. Starting fresh conversation.")
                logger.info(f"Session reset for channel {channel_id}")
            else:
                await message.reply("ℹ️ No active session to clear.")
            return

        kagent_a2a_url = os.getenv("KAGENT_A2A_URL")
        if not kagent_a2a_url:
            await message.reply("⚠️ Missing `KAGENT_A2A_URL` in `.env`.")
            return

        try:
            await message.channel.typing()
            if message.channel in SET:
                response = "This channel is already subscribed."
            else:
                response = "You've successfully subscribed to my newsletter. Rest assured, I will keep you fully updated on every single error."
                SET.add(message.channel)
            if len(response) <= MAX_LENGTH:
                await message.channel.send(response)
            else:
                chunks = split_by_lines(response)
                await message.channel.send(chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(chunk)

        except Exception as e:
            logger.error(f"Error: {e}")
            await message.reply(f"❌ Error while talking to Kagent: {e}")
