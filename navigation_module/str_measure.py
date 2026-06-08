import time
import sys

try:
    import starlink_grpc
except ImportError:
    print("Error: 'starlink-grpc-core' library not found.")
    print("Please run: pip install starlink-grpc-core")
    sys.exit(1)

def run_frequency_and_logger_test():
    log_filename = "dish_responses_log.txt"
    
    print("====================================================")
    print("  Starlink Dish API Frequency Analyzer & Logger     ")
    print("====================================================\n")
    print("Connecting to Dish at 192.168.100.1:9200...")
    
    try:
        # Initial connection check
        starlink_grpc.get_status()
        print("Successfully connected to Dish API!")
        print(f"Logging all raw responses to: {log_filename}\n")
    except Exception as e:
        print(f"Connection Failed: {e}")
        return

    print(f"{'Update #':<10}{'Delta Time (s)':<18}{'Instantaneous Hz':<18}")
    print("-" * 48)

    counter = 0
    last_time = time.perf_counter()

    # Open the log file in append mode
    with open(log_filename, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n--- NEW TEST SESSION STARTED AT {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        
        while True:
            try:
                # Fetch the live status object
                status_response = starlink_grpc.get_location()
                
                current_time = time.perf_counter()
                counter += 1
                
                # Calculate network timing statistics
                delta_time = current_time - last_time
                
                if delta_time > 0:
                    hz = 1.0 / delta_time
                    print(f"{counter:<10}{delta_time:<18.4f}{hz:<18.2f}")
                else:
                    print(f"{counter:<10}{delta_time:<18.4f}{'Too Fast':<18}")
                
                # Write the exact response payload to the file
                log_file.write(f"\n=========================================\n")
                log_file.write(f"RECORD #{counter} | Timestamp: {time.strftime('%H:%M:%S')} | Delta: {delta_time:.4f}s\n")
                log_file.write(f"=========================================\n")
                log_file.write(str(status_response)) # Converts protobuf object to readable text format
                log_file.write("\n")
                
                # Flush to disk immediately so you can inspect the file while it runs
                log_file.flush()
                
                last_time = current_time

            except KeyboardInterrupt:
                print("\n\nTesting stopped by user.")
                log_file.write(f"\n--- TEST SESSION STOPPED BY USER ---\n")
                break
            except Exception as e:
                print(f"\n[Warning] Packet dropped or network error: {e}")
                log_file.write(f"\n[ERROR AT RECORD #{counter+1}]: {e}\n")
                log_file.flush()
                last_time = time.perf_counter()
                time.sleep(0.1)

if __name__ == "__main__":
    run_frequency_and_logger_test()