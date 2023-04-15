import tiktoken


class TokenTester:
    def __init__(self, engine: str = "gpt-3.5-turbo"):
        self.tokenizer = tiktoken.encoding_for_model(engine)

    def tokenize(self, text):
        return self.tokenizer.encode(text, allowed_special='all')

    def get_token_count(self, text):
        tokens = self.tokenize(text)
        return len(tokens)
