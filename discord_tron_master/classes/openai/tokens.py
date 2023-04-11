from tiktoken import Tokenizer
from tiktoken.tokenizer import Tokenizer as OpenAITokenizer

text = "This is a sample text to check the token count."

tokenizer = OpenAITokenizer()
tokens = tokenizer.tokenize(text)

token_count = len(tokens)
print(f"Token count: {token_count}")

class TokenTester:
    def tokenize(self, text):
        return tokenizer.tokenize(text)

    def get_token_count(self, text):
        tokens = self.tokenize(text)
        return len(tokens)
    
    def truncate_conversation_history(conversation_history, new_prompt, max_tokens=2048):
        tokenizer = OpenAITokenizer()
    
        # Calculate tokens for new_prompt
        new_prompt_tokens = tokenizer.tokenize(new_prompt)
        new_prompt_token_count = len(new_prompt_tokens)
    
        if new_prompt_token_count >= max_tokens:
            raise ValueError("The new prompt alone exceeds the maximum token limit.")
    
        # Calculate tokens for conversation_history
        conversation_history_token_counts = [len(tokenizer.tokenize(entry)) for entry in conversation_history]
        total_tokens = sum(conversation_history_token_counts) + new_prompt_token_count
    
        # Truncate conversation history if total tokens exceed max_tokens
        while total_tokens > max_tokens:
            conversation_history.pop(0)  # Remove the oldest entry
            conversation_history_token_counts.pop(0)  # Remove the oldest entry's token count
            total_tokens = sum(conversation_history_token_counts) + new_prompt_token_count
    
        return conversation_history