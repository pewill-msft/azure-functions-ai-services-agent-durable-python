import React, { useState, useEffect } from 'react';
import axios from 'axios';
import ReactMarkdown from 'react-markdown';
import '../ChatInterface.css'; // Import the CSS file

// Get API URL from environment variables. When unset (production on Static Web Apps),
// fall back to '/api' so requests are same-origin and routed through SWA's linked backend.
const API_URL = process.env.REACT_APP_API_URL || '/api';

const ChatInterface = () => {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false); // State to manage loading spinner
  
  const sendMessage = async () => {
    if (input.trim() === '') return;

    const newMessage = { role: 'user', content: `${input}` };
    setMessages([...messages, newMessage]);

    // Clear the input immediately after submitting the prompt
    setInput('');
    setLoading(true); // Show loading spinner

    try {
      const response = await axios.post(`${API_URL}/prompt`, JSON.stringify({ Prompt: input }), {
        headers: {
          'Content-Type': 'application/json'
        }
      });

      // Extract the response message from the backend
      const botMessageContent = `${response.data.message}`;
      const botMessage = { role: 'bot', content: botMessageContent };
      setMessages((prevMessages) => [...prevMessages, botMessage]);
    } catch (error) {
      console.error('Error sending message:', error);
    } finally {
      setLoading(false); // Hide loading spinner
    }
  };

  return (
    <div className="page-container">
      <div className="chat-title-container">
        <h1>Welcome to the GitHub Issues Assistant</h1>
      </div>
      <div className="chat-container">
        <div className="chat-history">
          {messages.map((msg, index) => (
            <div key={index} className={`chat-message ${msg.role}`}>
              <ReactMarkdown>{msg.content}</ReactMarkdown> {/* Use ReactMarkdown to render markdown */}
            </div>
          ))}
          {loading && <div className="loading-spinner"></div>} {/* Show loading spinner */}
        </div>
        <div className="chat-input">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyPress={(e) => e.key === 'Enter' && sendMessage()}
          />
          <button onClick={sendMessage}>Send</button>
        </div>
      </div>
    </div>
  );
};

export default ChatInterface;