""" auto_logger.py - Terminator Plugin to automatically log 'content' of individual
terminals to a designated logging directory """

import os
import re
import threading
import queue
import tempfile
import subprocess
from datetime import datetime
from gi.repository import Gtk, Vte, GLib
import terminatorlib.plugin as plugin
from terminatorlib.terminator import Terminator

AVAILABLE = ['AutoLogger']

class AutoLogger(plugin.Plugin):
    """ Automatically log terminal content with async I/O and unique terminal IDs """
    capabilities = ['terminal_menu']
    
    # Class-level variables to track terminals across all plugin instances
    _global_pty_to_terminal_id = {}
    _global_session_timestamp = None
    
    def __init__(self):
        plugin.Plugin.__init__(self)
        self.loggers = {}
        self.terminal_ids = {}
        self.terminal_counter = 0
        self.vte_version = Vte.get_minor_version()
        
        # Initialize global session timestamp if not set
        if AutoLogger._global_session_timestamp is None:
            AutoLogger._global_session_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        
        # Auto-logging configuration
        self.log_directory = os.path.join(tempfile.gettempdir(), "terminator_logs")
        
        # Setup log directory
        if not os.path.exists(self.log_directory):
            os.makedirs(self.log_directory, exist_ok=True)
        
        # Async logging setup
        self.write_queue = queue.Queue(maxsize=1000)
        self.sanitize_queue = queue.Queue(maxsize=1000)
        self._shutdown_writer = False
        self.writer_thread = threading.Thread(target=self._async_writer, daemon=True)
        self.sanitizer_thread = threading.Thread(target=self._async_sanitizer, daemon=True)
        self.writer_thread.start()
        self.sanitizer_thread.start()
        
        # Start monitoring for new terminals
        GLib.timeout_add(500, self._check_for_new_terminals)

    def _async_writer(self):
        """ Background thread for async file writing """
        open_files = {}
        
        while not self._shutdown_writer:
            try:
                item = self.write_queue.get(timeout=1.0)
                if item is None:
                    break
                
                if len(item) != 2:
                    self.write_queue.task_done()
                    continue
                
                filepath, content = item
                
                if not filepath or not content:
                    self.write_queue.task_done()
                    continue
                
                try:
                    if filepath not in open_files:
                        os.makedirs(os.path.dirname(filepath), exist_ok=True)
                        open_files[filepath] = open(filepath, 'a', encoding='utf-8', buffering=1)
                    
                    fd = open_files[filepath]
                    fd.write(content)
                    fd.flush()
                    
                except Exception:
                    if filepath in open_files:
                        try:
                            open_files[filepath].close()
                        except:
                            pass
                        del open_files[filepath]
                
                self.write_queue.task_done()
                
            except queue.Empty:
                continue
            except Exception:
                pass
        
        for fd in open_files.values():
            try:
                fd.close()
            except:
                pass

    def _async_sanitizer(self):
        """ Background thread for async sanitization using presidio """
        while not self._shutdown_writer:
            try:
                item = self.sanitize_queue.get(timeout=1.0)
                if item is None:
                    break
                
                if len(item) != 2:
                    self.sanitize_queue.task_done()
                    continue
                
                content, output_filepath = item
                
                if not content or not output_filepath:
                    self.sanitize_queue.task_done()
                    continue
                
                try:
                    # Use subprocess to call presidio sanitizer
                    process = subprocess.Popen(
                        ['/opt/presidio-secrets-sanitizer/venv/bin/python3', 
                         '/opt/presidio-secrets-sanitizer/sanitizer.py', 
                         '--stdin', '-o', output_filepath],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    
                    stdout, stderr = process.communicate(input=content, timeout=30)
                    
                    # If sanitization fails, fail silently - no sanitized log created
                    
                except (subprocess.TimeoutExpired, Exception):
                    # If sanitization fails, fail silently - no sanitized log created
                    pass
                
                self.sanitize_queue.task_done()
                
            except queue.Empty:
                continue
            except Exception:
                pass

    def _get_terminal_id(self, terminal):
        """ Generate or retrieve unique ID for terminal """
        vte_terminal = terminal.get_vte()
        
        # First check if we already have an ID for this VTE object
        if vte_terminal in self.terminal_ids:
            return self.terminal_ids[vte_terminal]
        
        # Get PTY information to check for duplicates
        try:
            pty_fd = vte_terminal.get_pty().get_fd()
            import ctypes
            import ctypes.util
            
            libc = ctypes.CDLL(ctypes.util.find_library('c'))
            ptsname = libc.ptsname
            ptsname.restype = ctypes.c_char_p
            ptsname.argtypes = [ctypes.c_int]
            
            pts_name = ptsname(pty_fd)
            if pts_name:
                pts_name = pts_name.decode('utf-8')
                
                # Check if we already have a terminal ID for this PTY path (globally)
                if pts_name in AutoLogger._global_pty_to_terminal_id:
                    terminal_id = AutoLogger._global_pty_to_terminal_id[pts_name]
                else:
                    # Create new terminal ID
                    if '/dev/pts/' in pts_name:
                        pts_num = pts_name.split('/dev/pts/')[-1]
                        terminal_id = f"terminal_{pty_fd}_pts{pts_num}_{AutoLogger._global_session_timestamp}"
                    else:
                        terminal_id = f"terminal_{pty_fd}_{AutoLogger._global_session_timestamp}"
                    # Store the mapping globally
                    AutoLogger._global_pty_to_terminal_id[pts_name] = terminal_id
            else:
                terminal_id = f"terminal_{pty_fd}_{AutoLogger._global_session_timestamp}"
                        
        except Exception:
            self.terminal_counter += 1
            terminal_id = f"terminal_{self.terminal_counter}_{AutoLogger._global_session_timestamp}"
        
        # Cache the result for this VTE object
        self.terminal_ids[vte_terminal] = terminal_id
        return terminal_id


    def _check_for_new_terminals(self):
        """ Check for new terminals """
        try:
            terminator = Terminator()
            for terminal in terminator.terminals:
                vte_terminal = terminal.get_vte()
                if vte_terminal not in self.loggers:
                    self._start_logging(terminal)
        except:
            pass
        return True

    def callback(self, menuitems, menu, terminal):
        """ Show logging status in menu """
        vte_terminal = terminal.get_vte()
        if vte_terminal in self.loggers:
            terminal_id = self._get_terminal_id(terminal)
            filepath = self.loggers[vte_terminal]["filepath"]
            item = Gtk.MenuItem.new_with_label(f"Logging: {terminal_id} -> {os.path.basename(filepath)}")
            item.set_sensitive(False)
            menuitems.append(item)

    def _get_content(self, vte_terminal, row_start, col_start, row_end, col_end):
        """ Get terminal content - compatible with different VTE versions """
        try:
            if self.vte_version < 72:
                content = vte_terminal.get_text_range(row_start, col_start, row_end, col_end,
                                                    lambda *a: True)
            else:
                content = vte_terminal.get_text_range_format(Vte.Format.TEXT, row_start, col_start, 
                                                           row_end, col_end)
            
            if content and content[0]:
                return content[0]
            return ""
        except:
            return ""


    def _looks_like_prompt(self, line_content):
        """ Simple detection of prompt lines """
        if not line_content:
            return False
        
        line = line_content.strip()
        return (line.startswith('└─#') or 
                line.endswith('$ ') or 
                line.endswith('# ') or
                line.startswith('┌──') or
                ('┌──' in line and '💀' in line))


    def _write_to_log(self, vte_terminal, text):
        """ Queue text for async writing to log file """
        try:
            if not text or not text.strip():
                return
            
            if vte_terminal not in self.loggers:
                return
                
            filepath = self.loggers[vte_terminal]["filepath"]
            if not filepath:
                return
            
            clean_text = text.strip()
            if clean_text:
                try:
                    self.write_queue.put((filepath, f"{clean_text}\n"), timeout=0.1)
                except queue.Full:
                    pass
        except Exception:
            pass

    def _on_contents_changed(self, vte_terminal):
        """ Handle terminal content changes - only log when command completes """
        try:
            if vte_terminal not in self.loggers:
                return
            
            cursor_pos = vte_terminal.get_cursor_position()
            if not cursor_pos or len(cursor_pos) != 2:
                return
                
            current_col, current_row = cursor_pos
            logger_info = self.loggers[vte_terminal]
            last_row = logger_info.get("last_row", -1)
            
            # Only check for command completion when cursor moves to new line
            if current_row <= last_row:
                return
            
            # Get current line content to check if it's a new prompt
            current_line = self._get_content(vte_terminal, current_row, 0, 
                                           current_row, vte_terminal.get_column_count())
            
            # Only log when we see a new prompt (indicating previous command completed)
            if current_line and self._looks_like_prompt(current_line.strip()):
                # Get all content from last logged position to current
                start_row = max(0, last_row if last_row >= 0 else 0)
                end_row = current_row
                
                new_content = self._get_content(vte_terminal, start_row, 0, 
                                              end_row, vte_terminal.get_column_count())
                
                if new_content:
                    lines = new_content.split('\n')
                    filtered_lines = []
                    skip_until_prompt = False
                    
                    for line in lines:
                        stripped = line.strip()
                        
                        # Check if this line starts a "context" command
                        if self._is_context_command(stripped):
                            skip_until_prompt = True
                            continue
                        
                        # If we're skipping context output, check if we hit a new prompt
                        if skip_until_prompt:
                            if self._looks_like_prompt(stripped):
                                skip_until_prompt = False
                                # Include the new prompt line
                                if not self._is_empty_prompt(stripped):
                                    filtered_lines.append(line.rstrip())
                            continue
                        
                        # Skip empty lines, session markers, and empty prompts
                        if stripped and not stripped.startswith('==='):
                            if not self._is_empty_prompt(stripped):
                                if not self._is_partial_command(stripped):
                                    filtered_lines.append(line.rstrip())
                    
                    if filtered_lines:
                        content_to_log = '\n'.join(filtered_lines) + '\n'
                        
                        try:
                            self.write_queue.put((logger_info["filepath"], content_to_log), timeout=0.1)
                            self.sanitize_queue.put((content_to_log, logger_info["sanitized_filepath"]), timeout=0.1)
                        except queue.Full:
                            pass
                
                # Update last row position only after logging
                logger_info["last_row"] = current_row
                
        except:
            pass

    def _is_partial_command(self, line_content):
        """ Check if this looks like a partial command being typed """
        if not line_content:
            return True
        
        # If it's a prompt line, it's complete
        if self._looks_like_prompt(line_content):
            return False
        
        # Check for common incomplete patterns
        stripped = line_content.strip()
        
        # Single characters or very short incomplete commands
        if len(stripped) <= 2 and not stripped.isdigit():
            return True
        
        # Lines that end with cursor or partial input indicators
        if stripped.endswith('_') or stripped.endswith('|'):
            return True
            
        return False

    def _is_empty_prompt(self, line_content):
        """ Check if this is just an empty prompt with no command, possibly with error code """
        if not line_content:
            return True
            
        stripped = line_content.strip()
        
        # Check for empty prompt patterns
        if stripped == '└─#':
            return True
        
        # Check for prompts with only error codes at the end
        # Pattern: └─# followed by spaces and then error indicator like "2 ⨯" or "1 ✗" etc.
        if stripped.startswith('└─#'):
            # Remove the prompt part
            after_prompt = stripped[3:].strip()
            
            # If nothing after prompt, it's empty
            if not after_prompt:
                return True
            
            # Check if it's only whitespace followed by error code patterns
            # Common patterns: "2 ⨯", "1 ✗", "130 ⨯", etc.
            error_pattern = r'^\s*\d+\s*[⨯✗×✘❌]\s*$'
            if re.match(error_pattern, after_prompt):
                return True
        
        # Check other common prompt formats with error codes
        # Pattern: ending with $ or # followed by spaces and error indicator
        for prompt_suffix in ['$ ', '# ']:
            if prompt_suffix in stripped:
                parts = stripped.rsplit(prompt_suffix, 1)
                if len(parts) == 2:
                    after_prompt = parts[1].strip()
                    if not after_prompt:
                        return True
                    # Check for error code pattern
                    error_pattern = r'^\s*\d+\s*[⨯✗×✘❌]\s*$'
                    if re.match(error_pattern, after_prompt):
                        return True
        
        return False

    def _is_context_command(self, line_content):
        """ Check if this line contains a command that starts with 'context' """
        if not line_content:
            return False
        
        stripped = line_content.strip()
        
        # Look for commands that start with "context" after prompt indicators
        # Handle various prompt formats
        if self._looks_like_prompt(stripped):
            # Extract command part after prompt
            if '└─#' in stripped:
                cmd_part = stripped.split('└─#', 1)[-1].strip()
            elif stripped.endswith('$ ') or stripped.endswith('# '):
                # Handle other prompt formats
                for prompt_end in ['$ ', '# ']:
                    if stripped.endswith(prompt_end):
                        cmd_part = stripped[:-len(prompt_end)].strip()
                        break
                else:
                    cmd_part = ""
            else:
                cmd_part = ""
            
            return cmd_part.startswith('context')
        
        # Also check if it's just a bare context command (without prompt)
        return stripped.startswith('context')





    def _start_logging(self, terminal):
        """ Start logging for a terminal with unique ID and async I/O """
        try:
            vte_terminal = terminal.get_vte()
            
            if vte_terminal in self.loggers:
                return
            
            terminal_id = self._get_terminal_id(terminal)
            original_logfile = f"{self.log_directory}/{terminal_id}_original.log"
            sanitized_logfile = f"{self.log_directory}/{terminal_id}.log"
            
            
            cursor_pos = vte_terminal.get_cursor_position()
            initial_col, initial_row = cursor_pos if cursor_pos and len(cursor_pos) == 2 else (0, 0)
            
            contents_handler = vte_terminal.connect('contents-changed', self._on_contents_changed)
            
            self.loggers[vte_terminal] = {
                "filepath": original_logfile,
                "sanitized_filepath": sanitized_logfile,
                "terminal_id": terminal_id,
                "last_col": initial_col,
                "last_row": initial_row,
                "contents_handler": contents_handler
            }
            
            try:
                if not os.path.exists(original_logfile) or os.path.getsize(original_logfile) == 0:
                    session_start = f"=== Terminal session started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                    self.write_queue.put((original_logfile, session_start), timeout=0.1)
                    self.sanitize_queue.put((session_start, sanitized_logfile), timeout=0.1)
                    
                    # Capture initial terminal content (including the current prompt)
                    initial_content = self._get_content(vte_terminal, 0, 0, 
                                                      vte_terminal.get_row_count(), 
                                                      vte_terminal.get_column_count())
                    
                    if initial_content:
                        lines = initial_content.split('\n')
                        filtered_lines = []
                        skip_until_prompt = False
                        
                        for line in lines:
                            stripped = line.strip()
                            
                            # Check if this line starts a "context" command
                            if self._is_context_command(stripped):
                                skip_until_prompt = True
                                continue
                            
                            # If we're skipping context output, check if we hit a new prompt
                            if skip_until_prompt:
                                if self._looks_like_prompt(stripped):
                                    skip_until_prompt = False
                                    # Include the new prompt line
                                    if not self._is_empty_prompt(stripped):
                                        filtered_lines.append(line.rstrip())
                                continue
                            
                            if stripped and not stripped.startswith('==='):
                                if not self._is_empty_prompt(stripped):
                                    if not self._is_partial_command(stripped):
                                        filtered_lines.append(line.rstrip())
                        
                        if filtered_lines:
                            initial_log = '\n'.join(filtered_lines) + '\n'
                            self.write_queue.put((original_logfile, initial_log), timeout=0.1)
                            self.sanitize_queue.put((initial_log, sanitized_logfile), timeout=0.1)
                            
            except (queue.Full, Exception):
                pass
                
        except Exception:
            pass

    def _stop_logging(self, vte_terminal):
        """ Stop logging for a terminal """
        if vte_terminal not in self.loggers:
            return
            
        try:
            vte_terminal.disconnect(self.loggers[vte_terminal]["contents_handler"])
            del self.loggers[vte_terminal]
            
            if vte_terminal in self.terminal_ids:
                del self.terminal_ids[vte_terminal]
            
        except:
            pass

    def unload(self):
        """ Clean up when plugin unloads """
        for vte_terminal in list(self.loggers.keys()):
            self._stop_logging(vte_terminal)
        
        self._shutdown_writer = True
        self.write_queue.put(None)
        self.sanitize_queue.put(None)
        
        try:
            self.writer_thread.join(timeout=2.0)
            self.sanitizer_thread.join(timeout=2.0)
        except:
            pass
