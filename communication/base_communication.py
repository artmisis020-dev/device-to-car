from typing import Callable
from communication.config import command_address, telemetry_address
import zmq
import json

class BaseCommunication:
    def __init__(self, service_id: str, socket_path: str):
        self.service_id = service_id
        self.socket_path = socket_path
        self.running = True
        self.context = zmq.asyncio.Context.instance()
        self.socket = self.context.socket(zmq.DEALER)
        self.socket.setsockopt(zmq.IDENTITY, service_id.encode())
        self.socket.connect(socket_path)
        self.commands: dict[str, Callable] = {
            "get_status": self.get_status,
            "restart": self.restart_service,
            "shutdown": self.shutdown_service,
        }
        self.command_path = command_address(service_id)
        self.telemetry_path = telemetry_address(service_id)

    def handle_command(self, command: dict):
        command_id= command.get("id")
        command_type= command.get("type")
        payload = command.get("payload", {})
        if not command_id or not command_type:
            print(f"[{self.service_id}] Invalid command received: {command}")
            raise ValueError("Command must contain 'id' and 'type' fields.")
        
        handler = self.commands.get(command_type)

        if not handler:
            print(f"[{self.service_id}] Unknown command type received: {command_type}")
            raise ValueError(f"Unknown command type: {command_type}")
        
        try:
            response = handler(**payload)
            reply = {
                "id": command_id,
                "status": "success",
                "response": response
            }
            return reply
        except Exception as e:
            reply = {
                "id": command_id,
                "status": "error",
                "message": str(e)
            }
            print(f"[{self.service_id}] Error handling command {command_id}: {e}")
            return reply

    def start(self):
        print(f"[{self.service_id}] Starting communication...")
        while self.running:
            try:
                raw = self.socket.recv_json(flags=zmq.NOBLOCK)
                command = json.loads(raw.decode())
                print(f"[{self.service_id}] Received command: {command}")
                reply = self.handle_command(command)
                self.socket.send_json(reply)
                # if command in self.commands:
                #     response = self.commands[command_name]()
                #     self.socket.send_json({"status": "success", "response": response})
                # else:
                #     self.socket.send_json({"status": "error", "message": "Unknown command"})
            except zmq.Again:
                print(f"[{self.service_id}] No command received, continuing...")
                continue  # No message received, continue the loop
        
    def get_status(self):
        # Placeholder for actual status retrieval logic
        return {"service_id": self.service_id, "status": "running"}
    def restart_service(self):
        # Placeholder for actual restart logic
        return {"service_id": self.service_id, "action": "restart initiated"}
    def shutdown_service(self):
        self.running = False
        # Placeholder for actual shutdown logic
        return {"service_id": self.service_id, "action": "shutdown initiated"}