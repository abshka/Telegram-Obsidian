import asyncio
import sys
from rich import print as rprint
from typing import Any, AsyncGenerator, Dict, Optional, Union

from telethon import TelegramClient, types
from telethon.errors import (
    AuthKeyError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    PeerIdInvalidError,
    RpcCallFailError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
)
from telethon.tl.types import Message

from src.config import Config, ExportTarget
from src.exceptions import ExporterError, TelegramConnectionError
from src.utils import logger


class TelegramManager:
    def __init__(self, config: Config):
        """Initialize Telegram manager with configuration."""
        self.config = config
        proxy_info = None
        if config.proxy_type and config.proxy_addr and config.proxy_port:
            proxy_scheme = config.proxy_type.lower()
            if proxy_scheme not in ['socks4', 'socks5', 'http']:
                logger.warning(f"Unsupported proxy type for Telethon: '{proxy_scheme}'. Ignoring.")
            else:
                proxy_info = (proxy_scheme, config.proxy_addr, config.proxy_port)
                logger.info(f"Using {proxy_scheme.upper()} proxy for Telethon: {config.proxy_addr}:{config.proxy_port}")
        self.client = TelegramClient(
            config.session_name,
            config.api_id,
            config.api_hash,
            device_model="Telegram Markdown Exporter",
            app_version="1.0.0",
            connection_retries=5,
            retry_delay=2,
            request_retries=5,
            proxy=proxy_info
        )
        self.entity_cache: Dict[str, Any] = {}
        self.client_connected = False

    async def connect(self) -> bool:
        """Connect and authenticate with Telegram."""
        from rich import print as rprint
        if self.client_connected:
            logger.info("Client already connected.")
            rprint("[bold green]***Client already connected***[/bold green]")
            return True

        rprint("[bold cyan]***Authorization***[/bold cyan]")
        logger.info("Connecting to Telegram...")
        if not self.config.api_id or not self.config.api_hash:
            raise TelegramConnectionError("API ID and API Hash must be provided in the configuration.")

        try:
            await self.client.connect()

            # Handle authentication if needed
            if not await self.client.is_user_authorized():
                await self._authenticate()

            # Verify authentication was successful
            if not await self.client.is_user_authorized():
                logger.critical("Authorization failed even after sign-in attempt.")
                raise TelegramConnectionError("Authorization failed.")

            # Log successful connection
            me = await self.client.get_me()
            username = getattr(me, 'username', None) or getattr(me, 'first_name', 'Unknown User')
            logger.info(f"Connected as: {username} (ID: {getattr(me, 'id', 'unknown')})")
            self.client_connected = True
            rprint(f"[bold green]Authorization successful![/bold green] [dim]({username})[/dim]")
            return True

        except (AuthKeyError, UserDeactivatedBanError) as e:
            logger.error(f"Authentication error: {e}. Session may be invalid or account banned.")
            raise TelegramConnectionError(f"Authentication error: {e}") from e
        except ConnectionError as e:
            logger.error(f"Network connection error: {e}.")
            raise TelegramConnectionError(f"Network error: {e}") from e
        except TelegramConnectionError:
            raise
        except Exception as e:
            logger.error(f"Unexpected connection error: {e}", exc_info=self.config.verbose)
            raise TelegramConnectionError(f"Unexpected error: {e}") from e

    async def _authenticate(self) -> bool:
        """Handle Telegram authentication flow."""
        from rich import print as rprint
        rprint("[bold cyan]***Authorization***[/bold cyan]")
        logger.info("Authorization required.")
        if not self.config.phone_number:
            logger.critical("Phone number not provided in config/env and session is invalid.")
            raise TelegramConnectionError("Phone number required for authorization.")

        try:
            # Request verification code
            await self.client.send_code_request(self.config.phone_number)
            rprint("[bold yellow]Enter the code you received in Telegram:[/bold yellow]", end=" ")
            code = input()
            await self.client.sign_in(self.config.phone_number, code)
            logger.info("Signed in successfully using code.")
            rprint("[bold green]Signed in successfully using code![/bold green]")
            return True
        except SessionPasswordNeededError:
            # Handle 2FA
            logger.info("Two-step verification (2FA) enabled.")
            rprint("[bold yellow]Enter your 2FA password:[/bold yellow]", end=" ")
            password = input()
            try:
                await self.client.sign_in(password=password)
                logger.info("Signed in successfully using 2FA password.")
                rprint("[bold green]Signed in successfully using 2FA password![/bold green]")
                return True
            except Exception as pwd_err:
                logger.critical(f"Failed to sign in with 2FA password: {pwd_err}")
                raise TelegramConnectionError(f"2FA sign-in failed: {pwd_err}") from pwd_err
        except Exception as e:
            logger.critical(f"Failed to sign in with code: {e}")
            raise TelegramConnectionError(f"Code sign-in failed: {e}") from e

    async def disconnect(self) -> bool:
        """Disconnect from Telegram. Returns True if successful."""
        # Always return a value instead of None
        if not self.client or not self.client_connected:
            logger.info("Telegram client was not connected.")
            return True

        try:
            from rich import print as rprint
            logger.info("Disconnecting Telegram client...")
            rprint("[bold cyan]Disconnecting Telegram client...[/bold cyan]")
            if self.client.is_connected():
                await self.client.disconnect()
            self.client_connected = False
            logger.info("Telegram client disconnected.")
            rprint("[bold green]Telegram client disconnected.[/bold green]")
            return True
        except Exception as e:
            logger.error(f"Error during client disconnection: {e}")
            return False

    async def resolve_entity(self, entity_identifier: Union[str, int]) -> Optional[Any]:
        """Resolve an entity ID, username, or link to a Telethon object."""
        entity_id_str = str(entity_identifier).strip()
        if not entity_id_str:
            logger.warning("Attempted to resolve an empty entity identifier.")
            return None

        # Check cache first
        if entity_id_str in self.entity_cache:
            return self.entity_cache[entity_id_str]

        # Try to resolve with retries
        for attempt in range(1, 3):
            try:
                # Attempt to get entity
                entity = await self._get_entity(entity_id_str)

                # Cache and return if successful
                if entity:
                    self.entity_cache[entity_id_str] = entity
                    return entity

                logger.warning(f"Entity '{entity_id_str}' resolved to None.")
                return None

            except FloodWaitError as e:
                logger.warning(f"Flood wait: {e.seconds}s. Waiting...")
                await asyncio.sleep(e.seconds + 1)
            except (UsernameNotOccupiedError, UsernameInvalidError, PeerIdInvalidError) as e:
                logger.error(f"Invalid entity '{entity_id_str}': {type(e).__name__}")
                return None
            except (ChannelPrivateError, ChatAdminRequiredError, UserNotParticipantError) as e:
                logger.error(f"Access error for '{entity_id_str}': {type(e).__name__}")
                return None
            except RpcCallFailError as e:
                logger.warning(f"API call failed for '{entity_id_str}': {e}")
                await asyncio.sleep(1 * attempt)
            except Exception as e:
                logger.error(f"Error resolving '{entity_id_str}': {e}",
                             exc_info=self.config.verbose)
                await asyncio.sleep(1 * attempt)

        logger.error(f"Failed to resolve '{entity_id_str}' after multiple attempts.")
        return None

    async def _get_entity(self, entity_id_str: str) -> Optional[Any]:
        """Get entity from Telegram API, handling numeric IDs."""
        try:
            return await self.client.get_entity(entity_id_str)
        except ValueError:
            # Try as numeric ID if string looks like a number
            if entity_id_str.lstrip('-').isdigit():
                numeric_id = int(entity_id_str)
                return await self.client.get_entity(numeric_id)
            raise

    async def count_messages(
        self,
        entity: Any,
        min_id: Optional[int] = None
    ) -> int:
        """
        Count total messages in an entity (chat/channel) for progress bar.
        """
        try:
            total = 0
            safe_min_id = min_id or 0
            async for _ in self.client.iter_messages(
                entity=entity,
                limit=None,
                offset_id=0,
                reverse=True,
                min_id=safe_min_id,
                wait_time=self.config.request_delay
            ):
                total += 1
            return total
        except Exception as e:
            logger.error(f"Failed to count messages: {e}")
            return 0

    async def fetch_messages(
        self,
        entity: Any,
        min_id: Optional[int] = None
    ) -> AsyncGenerator[Message, None]:
        """Fetch messages from an entity with pagination handling."""
        entity_name = getattr(entity, 'title',
                             getattr(entity, 'username',
                                    getattr(entity, 'id', 'unknown')))
        entity_id = getattr(entity, 'id', 'unknown')

        logger.info(f"Fetching messages from: {entity_name} (ID: {entity_id})")
        if min_id:
            logger.info(f"Starting from message ID: {min_id}")

        total_fetched = 0
        last_logged_count = 0
        log_interval = 500
        safe_min_id = min_id or 0

        try:
            # Fetch all messages in a single pass
            async for message in self._iter_filtered_messages(
                entity, safe_min_id
            ):
                total_fetched += 1
                if total_fetched - last_logged_count >= log_interval:
                    logger.info(f"[{entity_name}] Fetched {total_fetched} messages so far")
                    last_logged_count = total_fetched
                yield message

            logger.info(f"Finished fetching from {entity_name}: {total_fetched} messages total")

        except FloodWaitError as e:
            logger.warning(f"[{entity_name}] Flood wait: {e.seconds}s")
            await asyncio.sleep(e.seconds + 5)
            raise TelegramConnectionError(f"Flood wait received ({e.seconds}s)") from e
        except (ChannelPrivateError, ChannelInvalidError, ChatAdminRequiredError, UserNotParticipantError) as e:
            logger.error(f"[{entity_name}] Access error: {e}")
            raise TelegramConnectionError(f"Access error: {e}") from e
        except RpcCallFailError as e:
            logger.error(f"[{entity_name}] API error: {e}")
            raise TelegramConnectionError(f"API error: {e}") from e
        except Exception as e:
            logger.error(f"[{entity_name}] Error fetching messages: {e}",
                        exc_info=self.config.verbose)
            raise ExporterError(f"Fetch error: {e}") from e

    async def _iter_filtered_messages(self, entity, min_id: int):
        """Iterate through filtered messages, removing service messages."""
        async for message in self.client.iter_messages(
            entity=entity,
            limit=None,  # Use the provided limit directly
            offset_id=0,
            reverse=True,
            min_id=min_id,
            wait_time=self.config.request_delay
        ):
            # Skip non-Message objects and service messages
            if not isinstance(message, Message) or getattr(message, 'action', None):
                continue

            yield message

    async def run_interactive_selection(self) -> bool:
        """Run interactive dialog to select chats/channels for export."""
        # Ensure we're connected first
        if not self.client_connected:
            logger.info("Connecting to Telegram for interactive selection...")
            await self.connect()
            if not self.client_connected:
                return False

        # Show welcome message
        me = await self.client.get_me()
        username = getattr(me, 'username', getattr(me, 'first_name', 'User'))
        rprint(f"\n[bold green]--- Welcome, {username}! ---[/bold green]")
        rprint("[bold]Select chats or channels to export.[/bold]")

        # Interactive menu loop
        while True:
            rprint("\n[bold yellow]Options:[/bold yellow]")
            rprint(" [cyan]1.[/cyan] List recent dialogs (Chats, Channels, Users)")
            rprint(" [cyan]2.[/cyan] Enter ID/Username/Link manually")
            rprint(" [cyan]3.[/cyan] Finish selection and start export")
            rprint(" [cyan]4.[/cyan] Exit")

            rprint("[bold cyan]Choose an option (1-4):[/bold cyan]", end=" ")
            choice = input()

            if choice == '1':
                await self._list_and_select_dialogs()
            elif choice == '2':
                await self._select_dialog_manually()
            elif choice == '3':
                if not self.config.export_targets:
                    rprint("\n[bold red]No targets selected. Please select at least one target.[/bold red]")
                else:
                    rprint("\n[bold green]Finished selection. Starting export process...[/bold green]")
                    break
            elif choice == '4':
                rprint("[bold yellow]Exiting.[/bold yellow]")
                sys.exit(0)
            else:
                rprint("[bold red]Invalid choice. Please enter a number between 1 and 4.[/bold red]")

        return True

    async def _list_and_select_dialogs(self) -> bool:
        """List recent dialogs and let user select which to export."""
        rprint("\n[bold]Fetching recent dialogs...[/bold]")
        try:
            # Get recent dialogs
            dialogs = await self.client.get_dialogs(limit=20)
            if not dialogs:
                rprint("[bold yellow]No dialogs found.[/bold yellow]")
                return False

            # Display dialogs
            rprint("[bold underline]Recent Dialogs:[/bold underline]")
            dialog_map = {}
            for i, dialog in enumerate(dialogs):
                entity = dialog.entity
                entity_id = getattr(entity, 'id', 'N/A')
                entity_type = self._get_entity_type_name(entity)
                rprint(f" [cyan]{i+1}.[/cyan] {dialog.name} (Type: {entity_type}, ID: {entity_id})")
                dialog_map[i+1] = entity

            # Handle selection
            await self._process_dialog_selection(dialog_map)
            return True

        except Exception as e:
            logger.error(f"Failed to list dialogs: {e}", exc_info=self.config.verbose)
            rprint(f"[bold red]Error fetching dialogs: {e}[/bold red]")
            return False

    def _get_entity_type_name(self, entity) -> str:
        """Get user-friendly entity type name."""
        if isinstance(entity, types.User):
            return "User"
        elif isinstance(entity, types.Chat):
            return "Group"
        elif isinstance(entity, types.Channel):
            return "Channel/Group"
        else:
            return type(entity).__name__

    async def _process_dialog_selection(self, dialog_map: Dict[int, Any]) -> bool:
        """Process user selection of dialogs."""
        while True:
            rprint("[bold cyan]Enter numbers to add (e.g., 1, 3, 5), or 'c' to cancel:[/bold cyan]", end=" ")
            selection = input()

            if selection.lower() == 'c':
                break

            try:
                # Parse indices
                indices = [int(s.strip()) for s in selection.split(',') if s.strip()]
                if not indices:
                    continue

                # Add selected entities
                added_count = 0
                for index in indices:
                    if index in dialog_map:
                        entity = dialog_map[index]
                        target = self._create_export_target_from_entity(entity)
                        self.config.add_export_target(target)
                        rprint(f"[bold green]Added: {target.name or target.id}[/bold green]")
                        added_count += 1
                    else:
                        rprint(f"[bold red]Invalid selection: {index}[/bold red]")

                if added_count > 0:
                    break

            except ValueError:
                rprint("[bold red]Invalid input. Please enter numbers separated by commas.[/bold red]")

        return True

    def _create_export_target_from_entity(self, entity) -> ExportTarget:
        """Create an ExportTarget from a Telethon entity."""
        # Get entity details
        entity_id = getattr(entity, 'id', 0)
        name = getattr(entity, 'title',
                      getattr(entity, 'username',
                             str(entity_id)))

        # Create target with appropriate type
        target = ExportTarget(id=entity_id, name=name)

        if isinstance(entity, types.User):
            target.type = 'user'
        elif isinstance(entity, types.Chat):
            target.type = 'group'
        elif isinstance(entity, types.Channel):
            target.type = 'channel'
        else:
            target.type = 'unknown'

        return target

    async def _select_dialog_manually(self) -> bool:
        """Allow manual entry of chat ID, username, or link."""
        while True:
            # Get input
            rprint("[bold cyan]Enter Chat/Channel ID, @username, or t.me/ link (or 'c' to cancel):[/bold cyan]", end=" ")
            identifier = input()

            if identifier.lower() == 'c':
                break

            if not identifier:
                continue

            # Try to resolve entity
            rprint(f"[bold]Resolving '{identifier}'...[/bold]")
            entity = await self.resolve_entity(identifier)

            if entity:
                # Create and show target
                target = self._create_export_target_from_entity(entity)
                rprint(f"[bold green]Resolved: {target.name} (Type: {target.type}, ID: {target.id})[/bold green]")

                # Confirm addition
                rprint("[bold cyan]Add this target? (y/n):[/bold cyan]", end=" ")
                confirm = input().strip().lower()
                if confirm == 'y':
                    self.config.add_export_target(target)
                    rprint(f"[bold green]Added: {target.name or target.id}[/bold green]")
                    break
            else:
                rprint(f"[bold red]Could not resolve or access '{identifier}'. Check input and permissions.[/bold red]")

        return True

    def get_client(self) -> TelegramClient:
        """Get the underlying TelegramClient instance."""
        if not self.client:
            raise RuntimeError("TelegramManager not initialized properly.")
        return self.client
